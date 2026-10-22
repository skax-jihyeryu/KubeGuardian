import re
from pathlib import Path

from service.agent._common.prompts import load_prompt
from service.agent.investigator.v1.config import SPEC
from service.agent.investigator.v1.context import load_knowledge, render_board, render_facts
from service.model import Signal, SignalBoard
from service.model.report import Fact

KNOWLEDGE = Path(__file__).parents[4] / "app" / "service" / "knowledge"


def test_knowledge_includes_environment_and_all_runbooks_but_not_template():
    text = load_knowledge(KNOWLEDGE)

    assert "# 환경 설명" in text
    for name in ("service-unreachable", "partial-failure", "noise-filtering"):
        assert f"[지침: {name}]" in text
    assert "조사 지침 템플릿" not in text


def test_render_board_lists_detected_and_observing_only():
    board = SignalBoard(
        by_service={
            "gateway": [
                Signal(
                    rule_id="S04",
                    verdict="detected",
                    subject="dev/Service/gateway",
                    severity="critical",
                    values={"env": "ADMIN_SERVICE_URL"},
                ),
                Signal(rule_id="S01", verdict="passed", subject="dev/Service/gateway"),
            ],
            "agent": [Signal(rule_id="S02", verdict="observing", subject="dev/Service/agent")],
        }
    )

    text = render_board(board)

    assert "S04 서비스 주소 설정 오류" in text and "적발(critical)" in text
    assert "ADMIN_SERVICE_URL" in text
    assert "S02" in text and "관찰 중" in text
    assert "S01" not in text


def test_render_board_says_when_empty():
    assert render_board(SignalBoard()) == "(적발·관찰 중인 신호 없음)"


def test_render_facts_cites_evidence():
    text = render_facts([Fact(statement="엔드포인트 0개", evidence_ids=["ev-3"])])

    assert text == "- 엔드포인트 0개 [ev-3]"


def test_prompts_render_without_missing_variables():
    values = {
        "system": {
            "namespace": "dev",
            "per_issue_budget": "8",
            "total_budget": "20",
            "knowledge": "K",
        },
        "triage": {
            "board": "B",
            "facts": "F",
            "symptoms": "Y",
            "symptom": "S",
            "gate_path": "A",
        },
        "investigate": {"issue": "I", "runbook": "R", "facts": "F"},
        "report": {"issue": "I", "facts": "F"},
    }
    for name, kw in values.items():
        text = load_prompt(SPEC.prompts_dir, name, **kw)
        assert not re.search(r"\$[a-z_]", text), name


def test_render_symptoms_shows_scenario_step_and_evidence_id():
    from service.agent.investigator.v1.context import render_symptoms
    from service.model import Symptom

    failed = Symptom(
        scenario="chat", status="failed", failed_step=2, summary="요약", evidence_id="ev-0003"
    )

    assert render_symptoms([failed]) == "- chat · 2단계 실패 [ev-0003] · 요약"
    assert render_symptoms([]) == "(없음)"
