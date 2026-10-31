import json
from datetime import UTC, datetime

from judge import JudgeResult
from run import (
    EXIT_FAIL,
    EXIT_INVALID,
    EXIT_PASS,
    decide_exit,
    investigate_and_judge,
    save_ai_artifacts,
)
from service.agent.base import AgentResult, AgentStep, Usage
from service.model import Report

EXPECTED = {"expected_top1": "agent selector 오타", "root_cause": "selector 오타"}


def _result(status="done", conclusion="agent Service selector 오타"):
    report = Report(conclusion=conclusion, confidence="high", impact_scope="agent")
    return AgentResult(
        status=status,
        report=report,
        usage=Usage(llm_calls=3, input_tokens=100, output_tokens=20, tool_calls=2, seconds=4.5),
    )


def test_unsupported_conclusion_fails_even_when_judge_matches():
    result = _result()
    result.report = result.report.model_copy(update={"conclusion_supported": False})

    ai, _, judged = _run(result)

    assert judged and ai["judge"]["match"] is True
    assert ai["conclusion_supported"] is False
    assert ai["passed"] is False
    assert "근거" in ai["fail_reason"]


def test_ai_block_records_demoted_from_validate_step():
    result = _result()
    result.steps = [
        AgentStep(
            node="validate",
            started_at=datetime.now(UTC),
            detail={"demoted": ["가설 A"], "conclusion_supported": True},
        )
    ]

    ai, _, _ = _run(result)

    assert ai["demoted"] == ["가설 A"]
    assert ai["conclusion_supported"] is True


def test_ai_block_records_judgement_and_llm_confidence():
    result = _result()
    result.report = result.report.model_copy(
        update={
            "judgement": "candidate",
            "judgement_basis": "원인 리소스 미조회 · 근거 출처 1종(설정·상태)",
        }
    )

    ai, _, _ = _run(result)

    assert (ai["judgement"], ai["llm_confidence"]) == ("candidate", "high")
    assert ai["judgement_basis"].startswith("원인 리소스 미조회")


def _run(result, judge_result=None, expected=None):
    judge_result = judge_result or JudgeResult(match=True, reason="같은 원인")
    judged = []

    def judge(conclusion):
        judged.append(conclusion)
        return judge_result

    ai, report, _ = investigate_and_judge(
        expected or EXPECTED, investigate=lambda: result, judge=judge
    )
    return ai, report, judged


def test_ai_block_records_conclusion_judge_and_usage():
    ai, report, judged = _run(_result())

    assert judged == ["agent Service selector 오타"]
    assert ai["status"] == "done"
    assert ai["conclusion"] == "agent Service selector 오타"
    assert ai["expected_top1"] == EXPECTED["expected_top1"]
    assert ai["judge"] == {"match": True, "reason": "같은 원인"}
    assert ai["usage"]["input_tokens"] == 100 and ai["usage"]["output_tokens"] == 20
    assert ai["passed"] is True
    assert report["conclusion"] == "agent Service selector 오타"


def test_mismatch_fails_and_unjudged_is_not_passed():
    assert _run(_result(), JudgeResult(match=False, reason="다른 원인"))[0]["passed"] is False
    assert _run(_result(), JudgeResult(match=None, reason="채점 실패"))[0]["passed"] is False


def test_judge_failure_is_invalid_not_fail():
    ai, _, _ = _run(_result(), JudgeResult(match=None, reason="채점 실패: Timeout"))

    assert "채점 실패" in ai["invalid"]
    assert decide_exit(True, ai) == EXIT_INVALID
    assert decide_exit(True, {"passed": False}) == EXIT_FAIL
    assert decide_exit(True, {"passed": True}) == EXIT_PASS
    assert decide_exit(False, {"passed": True}) == EXIT_FAIL


GATE_C = {"gate_path": "C", "expected_top1": "", "root_cause": ""}


def test_expected_gate_c_passes_when_investigation_skipped():
    ai, report, judged = _run(None, expected=GATE_C)

    assert ai["status"] == "skipped" and ai["passed"] is True and judged == []


def test_expected_gate_c_fails_when_investigation_ran_without_judging():
    ai, report, judged = _run(_result(), expected=GATE_C)

    assert judged == []
    assert ai["passed"] is False
    assert "게이트 C" in ai["fail_reason"]


def test_steps_are_returned_for_saving():
    result = _result()
    result.steps = [AgentStep(node="triage", started_at=datetime.now(UTC))]

    _, _, steps = investigate_and_judge(
        EXPECTED, investigate=lambda: result, judge=lambda c: JudgeResult(match=True)
    )

    assert [s["node"] for s in steps] == ["triage"]


def test_failed_agent_is_not_judged():
    ai, report, judged = _run(
        AgentResult(status="failed", error="LLM 오류", usage=Usage(seconds=1.0))
    )

    assert judged == [] and report is None
    assert ai["status"] == "failed" and ai["error"] == "LLM 오류" and ai["passed"] is False


def test_gate_closed_means_no_investigation():
    ai, report, judged = _run(None)

    assert ai["status"] == "skipped" and ai["passed"] is False and report is None


def test_tracing_is_set_up_before_the_investigation_span_starts(monkeypatch):
    # 추적 설정이 span 시작 뒤에 되면 span이 no-op이라 trace_id가 항상 비어 버린다
    import run

    order = []
    monkeypatch.setattr(run, "setup_tracing", lambda: order.append("setup"))

    def investigate():
        order.append("investigate")
        return None

    investigate_and_judge(EXPECTED, investigate=investigate, judge=lambda c: None)

    assert order == ["setup", "investigate"]


def test_steps_json_is_saved_next_to_report(tmp_path):
    save_ai_artifacts(tmp_path, {"conclusion": "c"}, [{"node": "triage"}])

    assert json.loads((tmp_path / "steps.json").read_text()) == [{"node": "triage"}]
    assert json.loads((tmp_path / "report.json").read_text())["conclusion"] == "c"


def test_ai_block_records_must_check_and_cause_resource():
    report = Report.model_validate(
        {
            "conclusion": "c",
            "confidence": "high",
            "impact_scope": "agent",
            "issues": [
                {
                    "rank": 1,
                    "title": "t",
                    "impact": "core_path_broken",
                    "confidence": "high",
                    "cause_resource": {"kind": "Deployment", "name": "agent"},
                }
            ],
            "coverage": {"config": {"status": "checked", "summary": "s", "evidence_ids": ["ev-1"]}},
        }
    )
    result = AgentResult(status="done", report=report, usage=Usage())
    expected = {**EXPECTED, "must_check": {"config": "이미지 참조 확인", "logs": "로그 확인"}}

    ai, _, _ = _run(result, expected=expected)

    assert ai["cause_resource"] == {"kind": "Deployment", "name": "agent"}
    assert ai["must_check"]["config"]["status"] == "checked"
    assert ai["must_check"]["config"]["has_evidence"] is True
    assert ai["must_check"]["logs"]["status"] == "not_checked"
