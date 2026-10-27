from datetime import timedelta

import pytest

from core.config import ApiScenario
from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore, create_db_engine
from service.pipeline.check import run_check
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster
from tests.service.tools.fakes import FakeHttp


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _reader(case=None):
    return FixtureReader(*raw_cluster(case))


def test_healthy_cluster_is_path_c_and_stores_snapshot(engine):
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "C"
    assert CheckRunStore(engine).get_snapshot(out.run_id).namespace == "dev"
    assert out.board.by_service == {}


def test_a1_selector_typo_opens_path_a_and_stores_evidence(engine):
    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "A"
    assert out.gate.severity == "critical"
    s01 = next(s for s in out.gate.signals if s.rule_id == "S01")
    ev = EvidenceStore(engine).get(s01.evidence_id)
    assert ev.kind == "detector"
    assert ev.subject == "dev/Service/agent"
    assert ev.data["values"]["selector"] == {"app": "agnet"}
    assert EvidenceStore(engine).check_run_of(s01.evidence_id) == out.run_id
    assert [s.rule_id for s in out.board.by_service["agent"]] == ["S01"]


def test_a5_localhost_address_opens_path_a(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)
    assert (out.gate.path, [s.rule_id for s in out.gate.signals]) == ("A", ["S04"])


def test_h0_advice_only_is_path_c(engine):
    out = run_check(_reader("h01/detected"), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "C"
    assert {s.rule_id for s in out.signals if s.verdict == "detected"} == {"H01"}


def test_observations_carry_over_between_checks(engine):
    first = run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=NOW)
    assert first.gate.path == "C"  # 관찰 중
    later = NOW + timedelta(minutes=15)
    second = run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=later)
    assert [s.rule_id for s in second.gate.signals] == ["S02"]
    assert SqliteObservationStore(engine).get("S02", "dev/Service/agent") is not None


def test_previous_snapshot_feeds_restart_increase(engine):
    run_check(_reader("p06/past_restarts"), CONFIG, engine, trigger="schedule", now=NOW)
    out = run_check(
        _reader("p06/restarted_running"),
        CONFIG,
        engine,
        trigger="schedule",
        now=NOW + timedelta(minutes=5),
    )
    p06 = next(s for s in out.signals if s.rule_id == "P06" and s.subject.endswith("x2k9p/agent"))
    assert (p06.verdict, p06.values["previous_restartCount"]) == ("observing", 3)


# ── 증상 감지 (경로 B) ──


def _symptom_config(core_paths=None):
    return CONFIG.model_copy(
        update={
            "core_paths": core_paths or {"chat": ["gateway"]},
            "api_scenarios": [
                ApiScenario.model_validate(
                    {
                        "name": "chat",
                        "feature": "chat",
                        "steps": [
                            {"call": {"via": "gateway", "path": "/chat", "method": "GET"}},
                        ],
                    }
                )
            ],
        }
    )


def _http(status):
    return FakeHttp({("gateway", "/chat"): status})


def test_without_http_symptoms_are_not_checked(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW)
    assert (out.gate.path, out.symptoms) == ("C", [])


def test_rules_pass_and_scenario_fails_opens_path_b(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW, http=_http(500))
    assert (out.gate.path, out.gate.severity) == ("B", "critical")
    [sym] = out.gate.symptoms
    assert (sym.scenario, sym.failed_step) == ("chat", 1)
    ev = EvidenceStore(engine).get(sym.evidence_id)
    assert ev.kind == "app_api"
    assert ev.subject == "dev/Scenario/chat"
    assert EvidenceStore(engine).check_run_of(sym.evidence_id) == out.run_id


def test_passing_scenario_keeps_path_c_but_is_reported(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW, http=_http(200))
    assert out.gate.path == "C"
    assert [(s.scenario, s.failed_step) for s in out.symptoms] == [("chat", None)]


def test_signal_keeps_path_a_even_if_scenario_fails(engine):
    out = run_check(
        _reader("s01/detected"),
        _symptom_config(),
        engine,
        trigger="check",
        now=NOW,
        http=_http(500),
    )
    assert (out.gate.path, out.gate.severity) == ("A", "critical")  # 신호 critical, 증상 critical
    assert [s.scenario for s in out.gate.symptoms] == ["chat"]
    assert [s.failed_step for s in out.symptoms] == [1]


def test_user_symptom_opens_path_b_without_http(engine):
    out = run_check(
        _reader(), CONFIG, engine, trigger="investigate", now=NOW, user_symptom="채팅 안 돼요"
    )
    assert (out.gate.path, out.gate.severity) == ("B", "warning")


def test_missing_target_service_opens_path_b_critical(engine):
    objects, failed = raw_cluster()
    objects["Service"] = [x for x in objects["Service"] if x["metadata"]["name"] != "gateway"]
    reader = FixtureReader(objects, failed)
    out = run_check(reader, _symptom_config(), engine, trigger="check", now=NOW, http=_http(200))
    assert (out.gate.path, out.gate.severity) == ("B", "critical")
    assert [(x.scenario, x.status) for x in out.gate.symptoms] == [("chat", "failed")]


def test_blank_user_symptom_is_not_a_symptom(engine):
    out = run_check(_reader(), CONFIG, engine, trigger="investigate", now=NOW, user_symptom="  \n")
    assert out.gate.path == "C"
