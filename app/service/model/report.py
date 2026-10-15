"""조사 결과: 선별 항목·가설·보고서 (기획서 §6.4)."""

from typing import Literal

from pydantic import BaseModel

from service.model.signal import Severity

Impact = Literal["core_path_broken", "partial_users", "latent_risk", "improvement"]
Confidence = Literal["high", "medium", "low"]
HypothesisStatus = Literal["supported", "refuted", "inconclusive"]

_SEVERITY_BY_IMPACT: dict[Impact, Severity] = {
    "core_path_broken": "critical",
    "partial_users": "warning",
    "latent_risk": "info",
    "improvement": "info",
}


def severity_for(impact: Impact) -> Severity:
    """조사 후 심각도는 영향(impact)에서 정한다 (기획서 §6.6)."""
    return _SEVERITY_BY_IMPACT[impact]


class Issue(BaseModel):
    rank: int
    title: str
    impact: Impact
    confidence: Confidence
    evidence_ids: list[str] = []

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
    evidence_ids: list[str]


class CheckedScope(BaseModel):
    checked: list[str] = []
    not_checked: list[str] = []


class Report(BaseModel):
    conclusion: str
    confidence: Confidence
    impact_scope: str
    issues: list[Issue] = []
    preliminary_facts: list[Fact] = []
    evidence_ids: list[str] = []
    hypotheses: list[Hypothesis] = []
    checked_scope: CheckedScope = CheckedScope()
    actions: list[str] = []  # 권장 조치 (명령 수준, 자동 실행 안 함)
    verification: str = ""  # 재검증 방법
