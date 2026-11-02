"""보고서 도구는 저장된 조사 결과만 읽고 근거 소유권과 크기 제한을 지킨다."""

import json
from datetime import timedelta, timezone

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from core.config import KubeGuardianConfig
from core.db import create_db_engine
from core.db.tables import InvestigationRow
from core.guard.budget import BudgetTracker
from service.agent.history_qa.v1.agent import Seen
from service.agent.history_qa.v1.config import SPEC
from service.agent.history_qa.v1.tools import KNOWLEDGE_DIR, build_history_tools
from service.chat import answer_question
from tests.api.seed import (
    add_evidence,
    at,
    finish_investigation,
    make_run,
    open_incident,
    report,
    start_investigation,
)
from tests.service.agent.history_qa.fakes import ScriptedLLM


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


@pytest.fixture
def tools(engine):
    return {tool.name: tool for tool in build_history_tools(engine, KNOWLEDGE_DIR)}


def call(tools, name, **args):
    return tools[name].run(args, BudgetTracker(SPEC.budget))


def saved(engine, conclusion="quota exhausted", *, evidence_ids=None):
    run = make_run(engine)
    issue = open_incident(engine, run)
    evidence = add_evidence(engine, run)
    investigation = start_investigation(engine, run, issue, [])
    finish_investigation(
        engine,
        investigation,
        report(conclusion=conclusion, evidence_ids=evidence_ids or [evidence]),
    )
    return investigation, run, issue, evidence


def test_search_reports_empty_and_filter_by_owner_keyword_and_time(tools, engine):
    assert call(tools, "search_reports")["reports"] == []
    rid, run, issue, evidence = saved(engine)
    saved(engine, "different cause")
    found = call(tools, "search_reports", issue_id=issue, run_id=run, keyword="quota")
    assert found["total"] == 1
    row = found["reports"][0]
    assert (row["id"], row["run_id"], row["issue_id"]) == (rid, run, issue)
    assert evidence in row["evidence_ids"]
    assert row["conclusion"] == "quota exhausted"
    assert call(tools, "search_reports", since=at(100).isoformat())["reports"] == []
    assert call(tools, "search_reports", run_id=run, issue_id=999)["reports"] == []


def test_search_reports_until_bounds_start_time(tools, engine):
    rid, *_ = saved(engine)

    assert (
        call(tools, "search_reports", until=(at(0) - timedelta(days=30)).isoformat())["reports"]
        == []
    )
    found = call(tools, "search_reports", until=(at(0) + timedelta(days=30)).isoformat())
    assert [r["id"] for r in found["reports"]] == [rid]
    kst = (at(0) + timedelta(days=30)).astimezone(timezone(timedelta(hours=9)))
    assert call(tools, "search_reports", until=kst.isoformat())["total"] == 1


@pytest.mark.parametrize(
    "args",
    [
        {"report_id": 0},
        {"report_id": -1},
        {"report_id": "R-1"},
        {"report_id": 2**64},
        {"report_id": True},
    ],
)
def test_report_id_validation(tools, args):
    assert call(tools, "get_report", **args)["error"] == "VALIDATION_ERROR"


def test_get_report_checks_ownership_and_missing(tools, engine):
    rid, run, issue, _ = saved(engine)
    assert call(tools, "get_report", report_id=rid)["report"]["actions"] == []
    assert call(tools, "get_report", report_id=rid, issue_id=issue, run_id=run)["ok"]
    for args in [
        {"report_id": 999},
        {"report_id": rid, "issue_id": 999},
        {"report_id": rid, "run_id": 999},
    ]:
        assert call(tools, "get_report", **args) == {"ok": False, "error": "NOT_FOUND"}


def test_invalid_report_is_unavailable_without_crashing_search(tools, engine):
    rid, _, _, _ = saved(engine)
    with Session(engine) as session, session.begin():
        session.get(InvestigationRow, rid).report = {"old_schema": True}
    assert call(tools, "get_report", report_id=rid)["error"] == "REPORT_UNAVAILABLE"
    assert call(tools, "search_reports")["reports"][0]["report_invalid"] is True


def test_report_mask_size_and_foreign_evidence_are_filtered(tools, engine):
    foreign = add_evidence(engine, make_run(engine))
    rid, run, issue, own = saved(
        engine, "password=secret-value " + "큰 내용" * 4000, evidence_ids=[foreign, "ev-9999"]
    )
    found = call(tools, "get_report", report_id=rid)
    assert "secret-value" not in json.dumps(found)
    assert len(json.dumps(found, ensure_ascii=False)) <= 6000
    assert foreign not in json.dumps(found)
    assert "ev-9999" not in json.dumps(found)
    assert found["truncated"] is True
    seen = Seen()
    seen.collect("get_report", found)
    assert foreign not in seen.evidence
    assert seen.runs == {run} and seen.issues == {issue}


def test_report_result_ids_flow_through_answer_validation(engine):
    rid, run, issue, ev = saved(engine)
    answer = json.dumps(
        {
            "sentences": [
                {
                    "text": "원인 확인",
                    "kind": "fact",
                    "evidence_ids": [ev],
                    "issue_ids": [issue],
                    "run_ids": [run],
                }
            ]
        }
    )
    llm = ScriptedLLM(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"name": "get_report", "args": {"report_id": rid}, "id": "r1"}],
            ),
            "끝",
            answer,
        ]
    )
    reply = answer_question(
        "보고서 원인?",
        engine=engine,
        config=KubeGuardianConfig(target={"namespace": "dev"}),
        llm_factory=lambda _: llm,
        now=lambda: at(70),
    )
    sentence = reply.answer.sentences[0]
    assert sentence.evidence_ids == [ev]
    assert sentence.issue_ids == [issue] and sentence.run_ids == [run]
    assert "추정" not in sentence.text


def test_search_limit_reports_total_and_ignores_reportless_investigations(tools, engine):
    from core.db import InvestigationStore

    for index in range(3):
        saved(engine, f"cause {index}")
    run = make_run(engine)
    investigation = start_investigation(engine, run, None, [])
    finish_investigation(engine, investigation, None, status="failed")
    result = call(tools, "search_reports", limit=1)
    assert result["total"] == 3
    assert len(result["reports"]) == 1
    assert result["truncated"] is True
    assert result["reports"][0]["conclusion"] == "cause 2"
    seen = Seen()
    seen.collect("search_reports", result)
    assert seen.issues == {result["reports"][0]["issue_id"]}
    assert result["reports"][0]["run_id"] in seen.runs
    assert InvestigationStore(engine).get(investigation).status == "failed"


def test_report_tool_calls_are_read_only_and_budgeted(tools, engine):
    from sqlalchemy import func, select

    from core.db.tables import Base

    rid, _, _, _ = saved(engine)

    def counts():
        with Session(engine) as session:
            return [
                session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            ]

    before = counts()
    budget = BudgetTracker(SPEC.budget)
    assert tools["search_reports"].run({}, budget)["ok"]
    assert tools["get_report"].run({"report_id": rid}, budget)["ok"]
    assert budget.tool_calls == 2
    assert counts() == before
    assert call(tools, "search_reports", issue_id=True)["error"] == "VALIDATION_ERROR"
    assert call(tools, "search_reports", limit=51)["error"] == "VALIDATION_ERROR"


def test_oversized_evidence_identifier_cannot_overflow_sqlite(tools, engine):
    rid, _, _, _ = saved(engine, evidence_ids=["ev-" + "9" * 30])
    assert call(tools, "get_report", report_id=rid)["evidence_ids"] == []


def test_report_keyword_matches_decoded_korean_and_literal_wildcards(tools, engine):
    rid, _, _, _ = saved(engine, "할당량 100% 소진")
    saved(engine, "할당량 1000 소진")
    found = call(tools, "search_reports", keyword="할당량 100%")
    assert [row["id"] for row in found["reports"]] == [rid]
