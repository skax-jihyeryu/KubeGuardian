from datetime import UTC, datetime, timedelta

import pytest

from core.db import CheckRunStore, InvestigationStore, create_db_engine
from service.agent.base import AgentResult, AgentStep, Usage
from service.model import Report

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _start(engine, incident_id=None):
    run = CheckRunStore(engine).create("schedule", NOW)
    store = InvestigationStore(engine)
    return (
        run,
        store,
        store.start(
            check_run_id=run,
            incident_id=incident_id,
            run_key="kg-worker-1-1",
            agent="investigator:v1",
            trigger="schedule",
            now=NOW,
        ),
    )


def _step(node, seconds, **kw):
    return AgentStep(
        node=node,
        started_at=NOW + timedelta(seconds=seconds),
        ended_at=NOW + timedelta(seconds=seconds + 1),
        **kw,
    )


def test_started_investigation_is_running_and_listed_for_its_check(engine):
    run, store, iid = _start(engine)

    [inv] = store.for_check(run)
    assert (inv.id, inv.status, inv.report, inv.agent) == (iid, "running", None, "investigator:v1")


def test_steps_are_written_as_they_happen_and_kept_in_order(engine):
    _, store, iid = _start(engine)
    sink = store.steps_sink(iid)

    sink.record(_step("preliminary", 0, evidence_ids=["ev-0001"]))
    assert [s.node for s in store.steps(iid)] == ["preliminary"]  # 끝나기 전에도 보인다
    sink.record(_step("tools", 3, tool="get_events", args={"since_minutes": 30}))

    steps = store.steps(iid)
    assert [s.node for s in steps] == ["preliminary", "tools"]
    assert steps[1].tool == "get_events" and steps[1].args == {"since_minutes": 30}
    assert sink.steps == steps  # 메모리에도 남아 결과(AgentResult.steps)로 돌려준다


def test_finished_investigation_keeps_report_usage_and_status(engine):
    _, store, iid = _start(engine, incident_id=None)
    report = Report(conclusion="selector 오타", confidence="high", impact_scope="채팅 전체")
    result = AgentResult(status="done", report=report, usage=Usage(llm_calls=4, input_tokens=9000))

    store.finish(iid, result, NOW + timedelta(seconds=80))

    inv = store.get(iid)
    assert (inv.status, inv.ended_at) == ("done", NOW + timedelta(seconds=80))
    assert inv.report.conclusion == "selector 오타" and inv.usage.llm_calls == 4


def test_failed_investigation_keeps_error(engine):
    _, store, iid = _start(engine)

    store.finish(iid, AgentResult(status="failed", error="LLM_CALL_FAILED"), NOW)

    inv = store.get(iid)
    assert (inv.status, inv.error, inv.report) == ("failed", "LLM_CALL_FAILED", None)


def test_investigations_of_an_incident(engine):
    from core.db import IncidentStore

    incident = IncidentStore(engine).open("fp", {}, "critical", 1, NOW)
    _, store, first = _start(engine, incident_id=incident)
    _, _, second = _start(engine, incident_id=incident)

    assert [i.id for i in store.for_incident(incident)] == [first, second]


# ── 리뷰 지적 (#98 / PR #112) ──


def test_start_closes_earlier_running_row_with_same_run_key(engine):
    run, store, first = _start(engine)  # run_key kg-worker-1-1

    second = store.start(
        check_run_id=run,
        incident_id=None,
        run_key="kg-worker-1-1",
        agent="investigator:v1",
        trigger="schedule",
        now=NOW + timedelta(minutes=11),
    )

    old, new = store.get(first), store.get(second)
    assert (old.status, old.error, old.ended_at) == (
        "failed",
        "INTERRUPTED",
        NOW + timedelta(minutes=11),
    )
    assert new.status == "running"


def test_start_keeps_finished_rows_of_same_run_key(engine):
    run, store, first = _start(engine)
    store.finish(first, AgentResult(status="done"), NOW)

    store.start(
        check_run_id=run,
        incident_id=None,
        run_key="kg-worker-1-1",
        agent="investigator:v1",
        trigger="schedule",
        now=NOW,
    )

    assert store.get(first).status == "done"


def test_finish_masks_secrets_in_report(engine):
    _, store, iid = _start(engine)
    report = Report(
        conclusion="DB 접속 postgres://app:secret@db/kg 실패, password=hunter22abc",
        confidence="high",
        impact_scope="전체",
        evidence_ids=["ev-0001"],
        judgement="candidate",
    )

    store.finish(iid, AgentResult(status="done", report=report), NOW)

    saved = store.get(iid).report
    assert "secret@" not in saved.conclusion and "hunter22abc" not in saved.conclusion
    assert (saved.judgement, saved.evidence_ids) == ("candidate", ["ev-0001"])


def test_finish_masks_a_missing_secret_name_even_though_it_hides_diagnostic_text(engine):
    # 마스킹이 안전한 기본값이다. "DB_PASSWORD가 누락됐다"는 진단 문장도 값처럼 가려진다
    _, store, iid = _start(engine)
    report = Report(
        conclusion="DB_PASSWORD: 누락",
        confidence="high",
        impact_scope="전체",
        evidence_ids=["ev-0001"],
    )

    store.finish(iid, AgentResult(status="done", report=report), NOW)

    assert store.get(iid).report.conclusion == "DB_PASSWORD: ***"


def test_record_survives_storage_error_and_keeps_memory_copy(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from core.db import investigations

    _, store, iid = _start(engine)
    sink = store.steps_sink(iid)

    def locked(*a, **k):
        raise OperationalError("insert", {}, Exception("database is locked"))

    monkeypatch.setattr(investigations, "Session", locked)

    sink.record(_step("preliminary", 0))

    assert [s.node for s in sink.steps] == ["preliminary"]


def test_invalid_report_json_does_not_break_the_list(engine):
    from sqlalchemy.orm import Session

    from core.db.tables import InvestigationRow

    run, store, good = _start(engine)
    bad = store.start(
        check_run_id=run,
        incident_id=None,
        run_key="bad",
        agent="investigator:v1",
        trigger="schedule",
        now=NOW,
    )
    with Session(engine) as s, s.begin():
        s.get_one(InvestigationRow, bad).report = {"unexpected": 1}

    by_id = {i.id: i for i in store.for_check(run)}

    assert by_id[bad].report is None and by_id[bad].report_invalid is True
    assert by_id[good].report_invalid is False


def test_latest_status_by_check_is_one_query_per_batch(engine):
    run_a, store, first = _start(engine)
    run_b = CheckRunStore(engine).create("schedule", NOW)
    second = store.start(
        check_run_id=run_a,
        incident_id=None,
        run_key="k2",
        agent="investigator:v1",
        trigger="schedule",
        now=NOW,
    )
    store.finish(second, AgentResult(status="done"), NOW)

    assert store.latest_status_by_check([run_a, run_b]) == {run_a: "done"}
    assert store.latest_status_by_check([]) == {}


def test_sweep_marks_old_running_rows_interrupted(engine):
    run, store, old = _start(engine)
    fresh = store.start(
        check_run_id=run,
        incident_id=None,
        run_key="k2",
        agent="investigator:v1",
        trigger="investigate",
        now=NOW + timedelta(minutes=9),
    )
    finished = store.start(
        check_run_id=run,
        incident_id=None,
        run_key="k3",
        agent="investigator:v1",
        trigger="investigate",
        now=NOW,
    )
    store.finish(finished, AgentResult(status="done"), NOW)

    swept = store.sweep_stale(NOW + timedelta(minutes=11), timedelta(minutes=10))

    assert swept == 1
    assert (store.get(old).status, store.get(old).error) == ("failed", "INTERRUPTED")
    assert store.get(fresh).status == "running" and store.get(finished).status == "done"


def test_start_stores_trace_gate_and_symptom_columns(engine):
    run = CheckRunStore(engine).create("schedule", NOW)
    store = InvestigationStore(engine)

    iid = store.start(
        check_run_id=run,
        incident_id=None,
        run_key="k",
        agent="investigator:v1",
        trigger="investigate",
        now=NOW,
        trace_id="a" * 32,
        gate_path="B",
        gate_reason="증상 실패",
        user_symptom="채팅이 안 돼요",
    )

    inv = store.get(iid)
    assert (inv.trace_id, inv.gate_path, inv.gate_reason, inv.user_symptom) == (
        "a" * 32,
        "B",
        "증상 실패",
        "채팅이 안 돼요",
    )
    _, other, plain = _start(engine)
    assert other.get(plain).trace_id is None  # 기본은 None
