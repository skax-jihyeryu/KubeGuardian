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


class Issue(BaseModel):
    rank: int
    title: str
    impact: Impact
    confidence: Confidence
    evidence_ids: list[str] = []
    cause_resource: ResourceRef | None = None  # 원인으로 지목한 리소스 (1순위는 필수, 모르면 None)

    @computed_field
    @property
    def severity(self) -> Severity:
        return severity_for(self.impact)


class Hypothesis(BaseModel):
    statement: str
    status: HypothesisStatus
    reason: str = ""
    evidence_ids: list[str] = []


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
    judgement_basis: SkipJsonSchema[str] = ""  # 예: "원인 리소스 설정 인용 · 근거 출처 2종(...)"
    impact_scope: str
    issues: list[Issue] = []
    preliminary_facts: list[Fact] = []
    evidence_ids: list[str] = []
    hypotheses: list[Hypothesis] = []
    coverage: Coverage = Field(default_factory=Coverage)
    actions: list[str] = []  # 권장 조치 (명령 수준, 자동 실행 안 함)
    verification: str = ""  # 재검증 방법

    @property
    def top_issue(self) -> Issue | None:
        """1순위 문제 (rank가 가장 작은 것). 출력·이슈 최종 심각도가 같은 기준을 쓴다."""
        return min(self.issues, key=lambda i: i.rank) if self.issues else None
