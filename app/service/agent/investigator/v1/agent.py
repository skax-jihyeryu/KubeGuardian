"""investigator v1: LangGraph로 사전 확인 → 선별 → 조사 → 보고서 → 근거 검증."""

from typing import ClassVar

from service.agent.base import AgentResult, AgentSpec, BaseAgent
from service.agent.investigator.v1.config import SPEC
from service.agent.registry import register
from service.model import InvestigationRequest

# 노드 수 × 도구 호출 상한(20) 여유. 상한은 코드가 먼저 끊으므로 이 값에 닿지 않는다
RECURSION_LIMIT = 100


@register
class InvestigatorV1(BaseAgent):
    spec: ClassVar[AgentSpec] = SPEC

    def _run(self, request: InvestigationRequest) -> AgentResult:
        from service.agent.investigator.v1.graph import build_graph

        state = build_graph(self).invoke({"request": request}, {"recursion_limit": RECURSION_LIMIT})
        return AgentResult(status=state["status"], report=state["report"])
