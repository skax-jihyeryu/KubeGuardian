import json
from typing import ClassVar

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.budget import Budget, BudgetTracker
from service.agent import registry
from service.agent._common.prompts import load_prompt
from service.agent._common.structured import invoke_structured, parse_structured
from service.agent.base import AgentResult, AgentSpec, BaseAgent, RunContext
from service.model import InvestigationRequest, Report, SignalBoard

REPORT = {
    "conclusion": "S04: gateway가 localhost를 호출",
    "confidence": "high",
    "impact_scope": "전체",
}


class EchoAgent(BaseAgent):
    """테스트용: 역할 'write'의 LLM이 준 보고서를 그대로 돌려준다."""

    spec: ClassVar[AgentSpec] = AgentSpec(name="echo", version="v1", models={"write": "fake"})

    def _run(self, request: InvestigationRequest) -> AgentResult:
        with self.step("write"):
            report = invoke_structured(
                self.llm("write"), Report, [("user", "보고서를 써라")], usage=self.ctx.usage
            )
        return AgentResult(status="done", report=report)


class BrokenAgent(EchoAgent):
    spec: ClassVar[AgentSpec] = AgentSpec(name="broken", version="v1", models={"write": "fake"})

    def _run(self, request):
        raise RuntimeError("내부 오류 token=abcdefgh12345678")


@pytest.fixture
def echo_registered():
    registry.register(EchoAgent)
    yield
    registry.unregister("echo:v1")


def _fake_llm(*responses: str):
    return lambda model: FakeListChatModel(responses=list(responses))


def _ctx() -> RunContext:
    return RunContext(
        run_id="run-1", namespace="dev", evidence=None, budget=BudgetTracker(Budget())
    )


REQ = InvestigationRequest(trigger="check", gate_path="A", signal_board=SignalBoard())


def test_registry_runs_agent_by_key_with_fake_llm(echo_registered):
    agent_cls = registry.get("echo:v1")
    agent = agent_cls(llm_factory=_fake_llm(json.dumps(REPORT)), tools=[], ctx=_ctx())

    result = agent.run(REQ)

    assert result.status == "done"
    assert result.report.conclusion.startswith("S04")
    assert [s.node for s in result.steps] == ["write"]
    assert result.steps[0].ended_at is not None
    assert result.usage.llm_calls == 1


def test_unexpected_error_becomes_failed_result_without_secret():
    result = BrokenAgent(llm_factory=_fake_llm("{}"), tools=[], ctx=_ctx()).run(REQ)

    assert result.status == "failed"
    assert "abcdefgh12345678" not in (result.error or "")


def test_same_key_cannot_be_registered_twice(echo_registered):
    with pytest.raises(ValueError, match="echo:v1"):
        registry.register(EchoAgent)


def test_unknown_agent_key_is_rejected():
    with pytest.raises(KeyError, match="nope:v9"):
        registry.get("nope:v9")


def test_llm_for_unknown_role_is_rejected(echo_registered):
    agent = EchoAgent(llm_factory=_fake_llm("{}"), tools=[], ctx=_ctx())

    with pytest.raises(KeyError, match="triage"):
        agent.llm("triage")


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(REPORT),
        "```json\n" + json.dumps(REPORT) + "\n```",
        "보고서입니다:\n" + json.dumps(REPORT, ensure_ascii=False) + "\n끝",
        "설명 {dev} ```kubectl get pods``` 다음이 결과:\n" + json.dumps(REPORT),
        json.dumps({**REPORT, "verification": "```bash\nkubectl get ep\n```"}),
        '{"conclusion": "줄\n바꿈", "confidence": "high", "impact_scope": "전체"}'.replace(
            "\\n", "\n"
        ),
    ],
)
def test_parse_structured_tolerates_fences_and_prose(text):
    assert parse_structured(text, Report).confidence == "high"


def test_invoke_structured_retries_once_then_succeeds():
    llm = FakeListChatModel(responses=["형식이 틀린 응답", json.dumps(REPORT)])

    assert invoke_structured(llm, Report, [("user", "x")]).impact_scope == "전체"


def test_invoke_structured_fails_after_retry():
    llm = FakeListChatModel(responses=["틀림", "또 틀림"])

    with pytest.raises(ServiceException) as exc:
        invoke_structured(llm, Report, [("user", "x")])
    assert exc.value.error_code is ErrorCode.LLM_OUTPUT_INVALID


def test_load_prompt_fills_variables_and_rejects_missing(tmp_path):
    (tmp_path / "system.md").write_text('$namespace 조사. 예시 {"a": 1}', encoding="utf-8")

    assert load_prompt(tmp_path, "system", namespace="dev") == 'dev 조사. 예시 {"a": 1}'
    with pytest.raises(KeyError, match="namespace"):
        load_prompt(tmp_path, "system")


def test_list_content_reply_is_parsed():
    from langchain_core.messages import AIMessage

    class ListContentLLM(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            return AIMessage(content=[{"type": "text", "text": json.dumps(REPORT)}])

    assert (
        invoke_structured(ListContentLLM(responses=["x"]), Report, [("user", "x")]).confidence
        == "high"
    )


def test_llm_call_error_is_normalized():
    class DownLLM(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            raise TimeoutError("timeout")

    with pytest.raises(ServiceException) as exc:
        invoke_structured(DownLLM(responses=["x"]), Report, [("user", "x")])
    assert exc.value.error_code is ErrorCode.LLM_CALL_FAILED
