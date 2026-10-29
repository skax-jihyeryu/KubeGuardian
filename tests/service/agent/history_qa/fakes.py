"""history_qa 테스트용 가짜 LLM."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedLLM(BaseChatModel):
    """각본의 응답을 차례로 돌려준다. 구조화 출력은 지원하지 않아 텍스트 파싱 경로를 탄다."""

    script: list[Any]
    calls: list[list] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        item = self.script.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        msg = item if isinstance(item, AIMessage) else AIMessage(content=item)
        return ChatResult(generations=[ChatGeneration(message=msg)])
