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
