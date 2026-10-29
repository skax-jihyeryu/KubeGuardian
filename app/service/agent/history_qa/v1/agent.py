"""history_qa v1: 이력 도구로 질문에 답한다."""

from typing import ClassVar

from service.agent.base import AgentResult, AgentSpec, BaseAgent
from service.agent.history_qa.v1.config import SPEC
from service.agent.registry import register
from service.model import ChatRequest, InvestigationRequest


@register
class HistoryQaV1(BaseAgent):
    spec: ClassVar[AgentSpec] = SPEC

    def _run(self, request: InvestigationRequest | ChatRequest) -> AgentResult:
        raise NotImplementedError
