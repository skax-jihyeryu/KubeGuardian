from datetime import UTC, datetime, timedelta

import pytest

from core.db import CheckResultStore, CheckRunStore, create_db_engine
from service.model import GateResult, Signal, Symptom

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
TTL = timedelta(seconds=270)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _run(engine, at=NOW):
    return CheckRunStore(engine).create("schedule", at)


def _signals():
    return [
        Signal(rule_id="S01", verdict="passed", subject="dev/Service/a"),
        Signal(rule_id="S01", verdict="passed", subject="dev/Service/b"),
        Signal(
            rule_id="W02",
            verdict="unknown",
            subject="dev",
            message="감지기 오류 ValueError: token=abcd1234secret 실패",
        ),
        Signal(
            rule_id="H01",
            verdict="detected",
            subject="dev/Deployment/agent",
            severity="info",
            values={"replicas": 3, "db_password": "hunter2"},
            evidence_id="ev-0007",
        ),
    ]


def test_started_check_is_running_and_phases_appear_one_by_one(engine):
    run = _run(engine)
    store = CheckResultStore(engine)

    store.start(run, NOW, excluded_rules=["W02"])
    assert store.get(run).status == "running"
    assert store.get(run).phases == []

    store.phase(run, "rules", NOW, NOW + timedelta(seconds=3), "적발 0 · 관찰 중 0 · 개선 권고 2")
    got = store.get(run)
    assert [(p.name, p.summary) for p in got.phases] == [
        ("rules", "적발 0 · 관찰 중 0 · 개선 권고 2")
    ]
    assert got.phases[0].ended_at == NOW + timedelta(seconds=3)
    assert got.excluded_rules == ["W02"]


def test_finish_keeps_gate_verdicts_with_pass_counts_and_symptoms(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)
    gate = GateResult(path="C", reason="장애 신호 없음 (개선 권고 1건)")
    symptoms = [
        Symptom(
            scenario="chat",
            status="failed",
            failed_step=2,
            summary="502",
            severity="critical",
            evidence_id="ev-0009",
        ),
        Symptom(scenario="login", status="passed", summary="ok"),
    ]

    store.finish(run, "done", NOW + timedelta(seconds=48), gate, _signals(), symptoms)

    got = store.get(run)
    assert (got.status, got.gate_path, got.gate_reason, got.first_severity) == (
        "done",
        "C",
        "장애 신호 없음 (개선 권고 1건)",
        None,
    )
    assert got.finished_at == NOW + timedelta(seconds=48)
    by_rule = {v["rule_id"]: v for v in got.verdicts}
    assert by_rule["S01"]["passed"] == 2 and by_rule["S01"]["items"] == []
    [unknown] = by_rule["W02"]["items"]
    assert unknown["verdict"] == "unknown" and "abcd1234secret" not in unknown["message"]
    [h01] = by_rule["H01"]["items"]
    assert (h01["evidence_id"], h01["values"]["replicas"], h01["values"]["db_password"]) == (
        "ev-0007",
        3,
        "***",
    )
    assert [
        (x["scenario"], x["status"], x["failed_step"], x["evidence_id"]) for x in got.symptoms
    ] == [
        ("chat", "failed", 2, "ev-0009"),
        ("login", "passed", None, None),
    ]


def test_finish_stores_first_severity_of_the_gate(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)

    store.finish(run, "done", NOW, GateResult(path="B", severity="critical", reason="증상"), [], [])

    assert store.get(run).first_severity == "critical"


def test_for_runs_returns_only_runs_that_have_a_result(engine):
    a, b = _run(engine), _run(engine)
    store = CheckResultStore(engine)
    store.start(a, NOW)

    got = store.for_runs([a, b])

    assert set(got) == {a} and store.get(b) is None
    assert store.for_runs([]) == {}


def test_running_past_the_lock_ttl_is_shown_as_halted(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)
    result = store.get(run)

    assert store.display_status(result, NOW + TTL - timedelta(seconds=1)) == "running"
    assert store.display_status(result, NOW + TTL + timedelta(seconds=1)) == "halted"


def test_a_long_manual_check_that_keeps_progressing_is_not_halted(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)
    store.phase(run, "rules", NOW, NOW + timedelta(seconds=250), "")
    store.phase(run, "symptoms", NOW + timedelta(seconds=250), NOW + timedelta(seconds=500), "")

    assert store.display_status(store.get(run), NOW + timedelta(seconds=600)) == "running"
    assert store.display_status(store.get(run), NOW + timedelta(seconds=800)) == "halted"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_finished_status_is_shown_as_is_even_when_old(engine, status):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)
    store.finish(run, status, NOW + timedelta(seconds=5), None, [], [])

    assert store.display_status(store.get(run), NOW + timedelta(days=1)) == status


def test_finish_records_the_error_type_of_a_failed_check(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)

    store.finish(run, "failed", NOW, None, [], [], error="RuntimeError")

    got = store.get(run)
    assert (got.status, got.error) == ("failed", "RuntimeError")


def test_rules_without_signals_still_get_an_entry(engine):
    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)

    store.finish(run, "done", NOW, None, _signals(), [], rules=["S01", "N01", "H01"])

    assert [(v["rule_id"], v["passed"], v["items"] == []) for v in store.get(run).verdicts] == [
        ("S01", 2, True),
        ("N01", 0, True),
        ("H01", 0, False),
        ("W02", 0, False),  # rules에 없어도 신호가 있으면 버리지 않는다
    ]


def test_failed_full_write_falls_back_to_status_and_finish_time(engine, monkeypatch):
    from core.db import check_results

    run = _run(engine)
    store = CheckResultStore(engine)
    store.start(run, NOW)

    def broken(signals, rules):
        raise ValueError("직렬화 실패")

    monkeypatch.setattr(check_results, "_verdicts", broken)
    store.finish(run, "done", NOW + timedelta(seconds=5), None, _signals(), [])

    got = store.get(run)
    assert (got.status, got.finished_at, got.verdicts) == ("done", NOW + timedelta(seconds=5), [])
