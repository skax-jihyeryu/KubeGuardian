"""investigator v1: LangGraph로 사전 확인 → 선별 → 조사 → 보고서 → 근거 검증."""

from traceback import format_exc
from typing import ClassVar

from core.guard.redact import redact_text
from core.log.logging import get_logging
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

        state: dict = {}
        try:
            stream = build_graph(self).stream(
                {"request": request}, {"recursion_limit": RECURSION_LIMIT}, stream_mode="values"
            )
            for state in stream:  # noqa: B007 - 마지막 상태가 필요하다
                pass
        except Exception as e:
            # 재조사 구간 실패는 1차 보고서로 끝낸다. 1차 조사 실패는 그대로 올린다
            if not (state.get("rechecked") and state.get("first_report")):
                raise
            with self.step("recheck_failed", recheck_failed=type(e).__name__):
                trace = redact_text(format_exc())
                get_logging().error(
                    f"run_id={self.ctx.run_id} 재조사 실패, 1차 보고서 유지\n{trace}"
                )
            return AgentResult(status=state["first_status"], report=state["first_report"])
        return AgentResult(status=state["status"], report=state["report"])
