from datetime import UTC, datetime

from judge import JudgeResult
from run import investigate_and_judge
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


def _run(result, judge_result=None):
    judge_result = judge_result or JudgeResult(match=True, reason="같은 원인")
    judged = []

    def judge(conclusion):
        judged.append(conclusion)
        return judge_result

    ai, report = investigate_and_judge(EXPECTED, investigate=lambda: result, judge=judge)
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
