import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from core.db import (
    AlertStore,
    CheckResultStore,
    CheckRunStore,
    EvidenceStore,
    IncidentStore,
    create_db_engine,
)
from core.db.tables import CheckResultRow, EvidenceRow
from core.guard.budget import Budget, BudgetTracker
from service.agent.history_qa.v1.tools import KNOWLEDGE_DIR, build_history_tools
from service.model import ClusterSnapshot, Evidence

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


@pytest.fixture
def tools(engine):
    return {t.name: t for t in build_history_tools(engine, KNOWLEDGE_DIR)}


def _budget(**kw):
    return BudgetTracker(Budget(per_issue_tool_calls=8, total_tool_calls=8, seconds=60, **kw))


def _snapshot(run_id, engine, pods):
    snap = ClusterSnapshot(
        namespace="dev",
        collected_at=NOW,
        resources={"Pod": pods, "Service": [{"metadata": {"name": "web"}}]},
        failed={"Event": "403"},
    )
    CheckRunStore(engine).save_snapshot(run_id, snap)


def _pod(name):
    return {"metadata": {"name": name}, "status": {"phase": "Running"}}


def test_list_runs_empty_db(tools):
    out = tools["list_runs"].run({}, _budget())
    assert out["runs"] == []
    assert out["total"] == 0


def test_list_runs_shows_incident_actions(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    inc = IncidentStore(engine).open("fp", {}, "high", run_id, NOW)
    out = tools["list_runs"].run({}, _budget())
    assert out["runs"][0]["incidents"] == [{"id": inc, "action": "opened"}]
    assert out["runs"][0]["started_at"].endswith("+09:00")


def test_get_snapshot_summary_then_filter(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod("agent-1"), _pod("web-1")])
    summary = tools["get_snapshot"].run({"run_id": run_id}, _budget())
    assert summary["kinds"] == {"Pod": 2, "Service": 1}
    assert summary["failed"] == {"Event": "403"}
    one = tools["get_snapshot"].run({"run_id": run_id, "kind": "Pod", "name": "agent"}, _budget())
    assert [i["metadata"]["name"] for i in one["items"]] == ["agent-1"]


def test_get_snapshot_kind_case_insensitive(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod("agent-1")])
    out = tools["get_snapshot"].run({"run_id": run_id, "kind": "pod"}, _budget())
    assert out["ok"] is True
    assert [i["metadata"]["name"] for i in out["items"]] == ["agent-1"]


def test_get_snapshot_unknown_kind_not_found(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod("agent-1")])
    out = tools["get_snapshot"].run({"run_id": run_id, "kind": "Nope"}, _budget())
    assert out == {"ok": False, "error": "NOT_FOUND", "kinds": ["Pod", "Service"]}


def test_get_snapshot_not_found(tools, engine):
    assert tools["get_snapshot"].run({"run_id": 9}, _budget()) == {
        "ok": False,
        "error": "NOT_FOUND",
    }


def test_get_snapshot_truncates_large(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod(f"pod-{i}") for i in range(500)])
    out = tools["get_snapshot"].run({"run_id": run_id, "kind": "Pod"}, _budget())
    assert out["truncated"] is True
    assert 0 < len(out["items"]) < 500
    assert len(json.dumps(out)) <= 6000 + 200


def test_get_evidence_masks(tools, engine):
    ev = Evidence(
        kind="log_excerpt",
        subject="dev/Pod/a",
        collected_at=NOW,
        source="GET /x",
        data={"line": "connect password=hunter2 failed"},
    )
    ev_id = EvidenceStore(engine).add(ev)
    out = tools["get_evidence"].run({"evidence_id": ev_id}, _budget())
    assert out["ok"] and "hunter2" not in json.dumps(out)
    assert tools["get_evidence"].run({"evidence_id": "ev-9999"}, _budget())["error"] == "NOT_FOUND"
    assert (
        tools["get_evidence"].run({"evidence_id": "ev-12x"}, _budget())["error"]
        == "VALIDATION_ERROR"
    )


def test_get_evidence_by_run_lists_without_data(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    store = EvidenceStore(engine, check_run_id=run_id)
    ids = [
        store.add(
            Evidence(kind="event", subject=f"s{i}", collected_at=NOW, source="src", data={"a": i})
        )
        for i in range(2)
    ]
    out = tools["get_evidence"].run({"run_id": run_id}, _budget())
    assert out["ok"] and out["run_id"] == run_id
    assert [e["id"] for e in out["evidences"]] == ids
    assert all(e["id"].startswith("ev-") for e in out["evidences"])
    assert set(out["evidences"][0]) == {"id", "kind", "subject", "source"}
    assert "data" not in json.dumps(out)


def test_get_evidence_by_run_empty(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    out = tools["get_evidence"].run({"run_id": run_id}, _budget())
    assert out == {"ok": True, "run_id": run_id, "evidences": []}


def test_get_evidence_needs_exactly_one_arg(tools):
    both = tools["get_evidence"].run({"evidence_id": "ev-0001", "run_id": 1}, _budget())
    neither = tools["get_evidence"].run({}, _budget())
    assert both["error"] == "VALIDATION_ERROR" and neither["error"] == "VALIDATION_ERROR"


def test_get_evidence_by_run_counts_budget(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    budget = _budget()
    tools["get_evidence"].run({"run_id": run_id}, budget)
    assert budget.tool_calls == 1


def test_list_issues_and_alerts_as_of_run(tools, engine):
    assert tools["list_issues"].run({}, _budget())["as_of_run"] is None
    assert tools["list_alerts"].run({}, _budget())["as_of_run"] is None
    runs = CheckRunStore(engine)
    runs.create("schedule", NOW)
    latest = runs.create("schedule", NOW)
    assert tools["list_issues"].run({}, _budget())["as_of_run"] == latest
    assert tools["list_alerts"].run({}, _budget())["as_of_run"] == latest


def test_get_snapshot_total_survives_truncation(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod(f"pod-{i}") for i in range(500)])
    out = tools["get_snapshot"].run({"run_id": run_id, "kind": "Pod"}, _budget())
    assert out["truncated"] is True and out["total"] == 500
    assert len(out["items"]) < 500


def test_get_snapshot_total_counts_after_name_filter(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod("agent-1"), _pod("agent-2"), _pod("web-1")])
    out = tools["get_snapshot"].run({"run_id": run_id, "kind": "Pod", "name": "agent"}, _budget())
    assert out["total"] == 2


def test_list_issues_keyword(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    store = IncidentStore(engine)
    a = store.open("a", {"services": ["llm-gateway"]}, "high", run_id, NOW)
    store.open("b", {"services": ["web"]}, "low", run_id, NOW)
    out = tools["list_issues"].run({"keyword": "LLM-GATEWAY"}, _budget())
    assert [i["id"] for i in out["issues"]] == [a]
    assert tools["list_issues"].run({"status": "resolved"}, _budget())["issues"] == []


def test_list_alerts_text_cut(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    inc = IncidentStore(engine).open("a", {}, None, run_id, NOW)
    AlertStore(engine).add(
        kind="opened", channel="C", text="가" * 1000, sent_at=NOW, incident_id=inc
    )
    out = tools["list_alerts"].run({"issue_id": inc}, _budget())
    assert len(out["alerts"][0]["text"]) == 500


def test_read_knowledge_list_and_read(tools):
    docs = tools["read_knowledge"].run({}, _budget())["docs"]
    assert docs[0] == "environment" and "_template" not in docs and "auth-failure" in docs
    out = tools["read_knowledge"].run({"name": "auth-failure"}, _budget())
    assert out["name"] == "auth-failure" and out["text"]
    assert tools["read_knowledge"].run({"name": "nope"}, _budget())["error"] == "NOT_FOUND"


def test_read_knowledge_rejects_path(tools):
    assert tools["read_knowledge"].run({"name": "../x"}, _budget())["error"] == "VALIDATION_ERROR"


def test_budget_exceeded(tools):
    budget = BudgetTracker(Budget(per_issue_tool_calls=8, total_tool_calls=1, seconds=60))
    assert tools["list_runs"].run({}, budget)["ok"]
    assert tools["list_runs"].run({}, budget) == {"ok": False, "error": "BUDGET_EXCEEDED"}
    assert budget.tool_calls == 1


def test_unknown_arg_rejected(tools):
    out = tools["list_runs"].run({"namespace": "x"}, _budget())
    assert out["error"] == "VALIDATION_ERROR" and out["fields"] == ["namespace"]


def test_tools_never_write(tools, engine):
    def evidence_rows():
        with engine.connect() as c:
            return c.scalar(select(func.count()).select_from(EvidenceRow))

    run_id = CheckRunStore(engine).create("schedule", NOW)
    _snapshot(run_id, engine, [_pod("a")])
    before = evidence_rows()
    calls = {
        "list_runs": {},
        "get_snapshot": {"run_id": run_id},
        "get_evidence": {"evidence_id": "ev-0001"},
        "list_issues": {},
        "list_alerts": {},
        "read_knowledge": {},
        "search_reports": {},
        "get_report": {"report_id": 1},
    }
    assert set(calls) == set(tools)
    budget = _budget()
    for name, args in calls.items():
        tools[name].run(args, budget)
    assert evidence_rows() == before


def test_since_requires_timezone(tools):
    assert (
        tools["list_runs"].run({"since": "2026-10-20T00:00:00"}, _budget())["error"]
        == "VALIDATION_ERROR"
    )
    since = (NOW - timedelta(days=1)).isoformat()
    assert tools["list_runs"].run({"since": since}, _budget())["ok"]


def test_list_issues_until_bounds_opened_at(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    store = IncidentStore(engine)
    old = store.open("old", {"services": ["a"]}, "high", run_id, NOW - timedelta(days=5))
    new = store.open("new", {"services": ["b"]}, "high", run_id, NOW)
    window = {
        "since": (NOW - timedelta(days=6)).isoformat(),
        "until": (NOW - timedelta(days=4)).isoformat(),
    }
    assert [i["id"] for i in tools["list_issues"].run(window, _budget())["issues"]] == [old]
    out = tools["list_issues"].run({"until": NOW.isoformat()}, _budget())
    assert {i["id"] for i in out["issues"]} == {old, new}
    assert (
        tools["list_issues"].run({"until": "2026-10-20T00:00:00"}, _budget())["error"]
        == "VALIDATION_ERROR"
    )


def test_list_alerts_until_bounds_sent_at(tools, engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    inc = IncidentStore(engine).open("a", {}, None, run_id, NOW)
    alerts = AlertStore(engine)
    for kind, when in (("old", NOW - timedelta(days=5)), ("new", NOW)):
        alerts.add(kind=kind, channel="C", text=kind, sent_at=when, incident_id=inc)
    out = tools["list_alerts"].run({"until": (NOW - timedelta(days=4)).isoformat()}, _budget())
    assert [a["kind"] for a in out["alerts"]] == ["old"]
    both = {"since": (NOW - timedelta(days=1)).isoformat(), "until": NOW.isoformat()}
    assert [a["kind"] for a in tools["list_alerts"].run(both, _budget())["alerts"]] == ["new"]


def test_until_is_documented_in_tool_schema(tools):
    for name in ("list_issues", "list_alerts"):
        assert "until" in tools[name].Input.model_fields


def test_list_runs_reports_total_before_limit(tools, engine):
    store = CheckRunStore(engine)
    store.create("schedule", NOW)
    latest = store.create("schedule", NOW + timedelta(hours=1))
    out = tools["list_runs"].run({"limit": 1}, _budget())
    assert out["total"] == 2
    assert out["truncated"] is True
    assert out["runs"][0]["id"] == latest
    assert out["runs"][0]["severity"] is None


def test_list_issues_opened_window_excludes_ongoing_and_end_boundary(tools, engine):
    runs = CheckRunStore(engine)
    incidents = IncidentStore(engine)
    end = NOW + timedelta(hours=1)
    for key, at in [("prior", NOW - timedelta(hours=1)), ("new", NOW), ("boundary", end)]:
        incidents.open(key, {}, "critical", runs.create("schedule", at), at)
    out = tools["list_issues"].run(
        {"since": NOW, "until": end, "until_exclusive": True, "date_basis": "opened"}, _budget()
    )
    assert out["ok"] is True
    assert out["total"] == 1
    assert [i["id"] for i in out["issues"]] == [2]
    ongoing = tools["list_issues"].run(
        {"since": NOW, "until": end, "date_basis": "ongoing"}, _budget()
    )
    assert ongoing["total"] == 1
    assert [i["id"] for i in ongoing["issues"]] == [1]


def test_list_runs_filters_trigger_and_returns_stored_severity(tools, engine):
    runs = CheckRunStore(engine)
    scheduled = runs.create("schedule", NOW)
    runs.create("ui", NOW + timedelta(minutes=1))
    CheckResultStore(engine).start(scheduled, NOW)
    with engine.begin() as connection:
        connection.execute(
            update(CheckResultRow)
            .where(CheckResultRow.run_id == scheduled)
            .values(status="done", first_severity="critical")
        )
    out = tools["list_runs"].run({"trigger": "schedule"}, _budget())
    assert out["total"] == 1
    assert out["runs"][0]["severity"] == "긴급"


def test_list_runs_does_not_label_partial_check_normal(tools, engine):
    run = CheckRunStore(engine).create("schedule", NOW)
    CheckResultStore(engine).start(run, NOW)
    with engine.begin() as connection:
        connection.execute(
            update(CheckResultRow)
            .where(CheckResultRow.run_id == run)
            .values(status="done", symptoms=[{"status": "unknown"}])
        )
    out = tools["list_runs"].run({}, _budget())
    assert out["runs"][0]["severity"] is None


@pytest.mark.parametrize("basis", ["overlap", "ongoing"])
def test_list_issues_empty_exclusive_window(tools, engine, basis):
    prior = NOW - timedelta(hours=1)
    run = CheckRunStore(engine).create("schedule", prior)
    IncidentStore(engine).open("prior", {}, "critical", run, prior)
    out = tools["list_issues"].run(
        {"since": NOW, "until": NOW, "until_exclusive": True, "date_basis": basis}, _budget()
    )
    assert out["total"] == 0
    assert out["issues"] == []
