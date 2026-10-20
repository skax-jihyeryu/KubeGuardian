"""investigator v1 그래프 상태와 구조화 출력 스키마 (상세설계 §2.2)."""

from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from service.model import InvestigationRequest, Report
from service.model.report import Fact, Issue


class TriageResult(BaseModel):
    issues: list[Issue] = Field(default_factory=list, max_length=3)
    runbook: str = ""  # 1순위 문제에 맞는 조사 지침 이름


class InvestigationState(TypedDict, total=False):
    request: InvestigationRequest
    facts: list[Fact]  # 사전 확인 사실
    triage: TriageResult
    messages: list[BaseMessage]  # 조사 중 LLM ↔ 도구 대화
    seen: list[str]  # 이번 조사에서 실제로 받은 근거 번호 (검증 기준)
    limited: bool  # 예산·시간 상한에 닿음
    report: Report | None
    report_attempts: int
    status: Literal["running", "done", "incomplete"]
