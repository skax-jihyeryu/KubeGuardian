"""조사 결과: 선별 항목·가설·보고서 (기획서 §6.4)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field
from pydantic.json_schema import SkipJsonSchema

from service.model.signal import Severity

Impact = Literal["core_path_broken", "partial_users", "latent_risk", "improvement"]
Confidence = Literal["high", "medium", "low"]  # LLM이 스스로 매긴 값. 화면·알림에는 쓰지 않는다
# 판단 수준: 코드가 근거로 정한다 (기획서 §6.2 후보·확정, #106). 화면·알림에 보이는 값
Judgement = Literal["confirmed", "candidate", "guess"]  # 확정 / 후보 / 추정
JUDGEMENT_LABEL: dict[str, str] = {"confirmed": "확정", "candidate": "후보", "guess": "추정"}
HypothesisStatus = Literal["supported", "refuted", "inconclusive"]
Area = Literal["run", "config", "wiring", "logs"]  # 구동·설정·연결·로그 (eval must_check와 같은 키)
AREAS: tuple[Area, ...] = ("run", "config", "wiring", "logs")
CoverageStatus = Literal["checked", "not_checked", "not_applicable"]

_SEVERITY_BY_IMPACT: dict[Impact, Severity] = {
    "core_path_broken": "critical",
    "partial_users": "warning",
    "latent_risk": "info",
    "improvement": "info",
}


def severity_for(impact: Impact) -> Severity:
    """조사 후 심각도는 영향(impact)에서 정한다 (기획서 §6.6)."""
    return _SEVERITY_BY_IMPACT[impact]


class ResourceRef(BaseModel):
    kind: str
    name: str


def same_resource(a: ResourceRef, b: ResourceRef) -> bool:
    """kind는 대소문자를 가리지 않는다 (LLM이 deployment처럼 쓰는 경우)."""
    return a.kind.lower() == b.kind.lower() and a.name == b.name


class ChainHop(BaseModel):
    service: ResourceRef  # 호출 지점의 리소스 식별자 (Pod/Deployment/Service 등)
    observation: str
    evidence_ids: list[str] = Field(default_factory=list)
    error_from_downstream: bool


class Issue(BaseModel):
    rank: int
    title: str
    impact: Impact
    confidence: Confidence
    evidence_ids: list[str] = []
    cause_resource: ResourceRef | None = None  # 원인으로 지목한 리소스 (1순위는 필수, 모르면 None)

    hypothesis_id: str | None = None

    @computed_field
    @property
    def severity(self) -> Severity:
        return severity_for(self.impact)


class HypothesisEvidence(BaseModel):
    evidence_id: str
    observation: str  # 해당 근거에서 직접 확인한 사실


class UnknownObservation(BaseModel):
    question: str
    check: str | None = None  # 도구로 확인할 수 있는 추가 관측
    reason: str = ""  # 확인하지 못했거나 도구로 확인할 수 없는 이유


class Hypothesis(BaseModel):
    statement: str
    status: HypothesisStatus
    reason: str = ""
    evidence_ids: list[str] = []  # 기존 소비자용 인용 목록
    id: str | None = None
    resource: ResourceRef | None = None
    support: list[HypothesisEvidence] = Field(default_factory=list)
    refute: list[HypothesisEvidence] = Field(default_factory=list)
    unknown: list[UnknownObservation] = Field(default_factory=list)


class Fact(BaseModel):
    """가설 없이 확인한 사실 (사전 확인)."""

    statement: str
    evidence_ids: list[str] = Field(min_length=1)  # 사실 문장은 근거를 1개 이상 인용한다


class AreaCoverage(BaseModel):
    status: CoverageStatus = "not_checked"
    summary: str = ""  # 확인함: 본 것 / 확인하지 못함·해당 없음: 사유
    evidence_ids: list[str] = []


def _missing_area() -> AreaCoverage:
    return AreaCoverage(summary="보고서에 없음")


class Coverage(BaseModel):
    """영역별 확인 범위. 빠진 영역은 not_checked, 모르는 키는 무시한다."""

    model_config = ConfigDict(extra="ignore")

    run: AreaCoverage = Field(default_factory=_missing_area)
    config: AreaCoverage = Field(default_factory=_missing_area)
    wiring: AreaCoverage = Field(default_factory=_missing_area)
    logs: AreaCoverage = Field(default_factory=_missing_area)


class Report(BaseModel):
    conclusion: str
    confidence: Confidence
    conclusion_supported: bool = True  # False = 결론이 인용한 근거가 없다 (검증기가 정한다)
    # 판단 수준은 코드가 붙인다 (core/guard/judgement.py). LLM 구조화 출력 스키마에서는 뺀다
    judgement: SkipJsonSchema[Judgement | None] = None
    # 예: "원인 리소스 설정 인용 · 근거 출처 2종(...) · 지지 가설 근거 2종"
    judgement_basis: SkipJsonSchema[str] = ""
    impact_scope: str
    issues: list[Issue] = []
    preliminary_facts: list[Fact] = []
    evidence_ids: list[str] = []
    hypotheses: list[Hypothesis] = []
    coverage: Coverage = Field(default_factory=Coverage)
    cause_chain: list[ChainHop] = Field(default_factory=list)
    actions: list[str] = []  # 권장 조치 (명령 수준, 자동 실행 안 함)
    verification: str = ""  # 재검증 방법

    @property
    def top_issue(self) -> Issue | None:
        """1순위 문제 (rank가 가장 작은 것). 출력·이슈 최종 심각도가 같은 기준을 쓴다."""
        return min(self.issues, key=lambda i: i.rank) if self.issues else None

    @property
    def linked_hypothesis(self) -> Hypothesis | None:
        """1순위 문제가 hypothesis_id로 가리키고 대상 리소스가 cause_resource와 같은 가설 하나."""
        return None if self.hypothesis_link_gap is not None else self._linked_candidate

    @property
    def _linked_candidate(self) -> Hypothesis | None:
        top = self.top_issue
        if top is None or not top.hypothesis_id:
            return None
        matches = [h for h in self.hypotheses if h.id == top.hypothesis_id]
        return matches[0] if len(matches) == 1 else None

    @property
    def hypothesis_link_gap(self) -> str | None:
        """1순위 문제가 가설을 가리켰는데 연결이 성립하지 않은 이유. 가리키지 않았으면 None."""
        top = self.top_issue
        if top is None or not top.hypothesis_id:
            return None
        if top.cause_resource is None:
            return "1순위 원인 리소스가 없어 가설과 연결할 수 없습니다"
        count = sum(h.id == top.hypothesis_id for h in self.hypotheses)
        if count != 1:
            return f"hypothesis_id {top.hypothesis_id}에 해당하는 가설이 {count}개입니다"
        hypothesis = self._linked_candidate
        assert hypothesis is not None
        if hypothesis.resource is None:
            return f"가설 {top.hypothesis_id}에 대상 리소스(resource)가 없습니다"
        if not same_resource(hypothesis.resource, top.cause_resource):
            return (
                f"가설 {top.hypothesis_id}의 대상 리소스가 1순위 원인 리소스"
                f" {top.cause_resource.kind}/{top.cause_resource.name}와 다릅니다"
            )
        return None
