"""investigator v1: LangGraph로 사전 확인 → 선별 → 조사 → 보고서 → 근거 검증."""

from typing import ClassVar

from service.agent.base import AgentResult, AgentSpec, BaseAgent
from service.agent.investigator.v1.config import SPEC
from service.agent.registry import register
from service.model import InvestigationRequest


@register
class InvestigatorV1(BaseAgent):
    spec: ClassVar[AgentSpec] = SPEC

    def _run(self, request: InvestigationRequest) -> AgentResult:
        raise NotImplementedError
