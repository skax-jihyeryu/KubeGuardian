"""investigator v1 그래프 상태와 구조화 출력 스키마 (상세설계 §2.2)."""

from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from service.model import InvestigationRequest, Report
from service.model.report import AREAS, Area, Fact, Issue


class TriageResult(BaseModel):
    issues: list[Issue] = Field(default_factory=list, max_length=3)
    runbook: str = ""  # 1순위 문제에 맞는 조사 지침 이름


class PlannedHypothesis(BaseModel):
    statement: str
    area: Area
    check: str  # 어느 영역의 무엇을 보면 지지·배제되는지


class InvestigationPlan(BaseModel):
    """문제 하나에 대한 가설(최대 3)과 영역을 볼 순서."""

    hypotheses: list[PlannedHypothesis] = Field(default_factory=list, max_length=3)
    order: list[Area] = Field(default_factory=lambda: list(AREAS))


class Observation(BaseModel):
    """도구 호출 하나 = 근거 하나. 멈춤 검사가 본 영역과 리소스를 가리는 데 쓴다."""

    evidence_id: str
    tool: str
    kind: str | None = None  # get_resource의 kind
    name: str | None = None  # get_resource의 name
    ok: bool = True  # 조회 실패(ok=False)는 본 것으로 세지 않는다


class InvestigationState(TypedDict, total=False):
    request: InvestigationRequest
    facts: list[Fact]  # 사전 확인 사실
    triage: TriageResult
    messages: list[BaseMessage]  # 조사 중 LLM ↔ 도구 대화
    seen: list[str]  # 이번 조사에서 실제로 받은 근거 번호 (검증 기준)
    limited: bool  # 예산·시간 상한에 닿음
    current_issue: int  # 조사 중인 문제의 triage.issues 인덱스
    plans: list[InvestigationPlan]  # 문제별 가설 계획 (조사한 순서)
    observations: list[Observation]
    rechecked: bool  # 빠진 영역 재조사를 이미 했다
    recheck: bool  # stop_check가 방금 재조사를 시켰다 (act로 되돌아간다)
    report: Report | None
    report_attempts: int
    status: Literal["running", "done", "incomplete"]
