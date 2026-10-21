"""AI 게이트 판정 (기획서 §3.1·§6.2)."""

from typing import Literal

from pydantic import BaseModel

from service.model.signal import Severity, Signal

GatePath = Literal["A", "B", "C"]  # A 규칙 적발 / B 규칙 통과 + 증상 / C 정상


class Symptom(BaseModel):
    """API 시나리오 한 건의 실행 결과. failed_step이 있으면 증상이다 (기획서 §7.3)."""

    scenario: str
    failed_step: int | None = None  # 처음 실패한 단계(1부터). 모두 성공이면 None
    summary: str  # 마스킹된 한 줄 요약
    severity: Severity | None = None  # 실패일 때만: core_paths 시나리오 critical, 그 밖 warning
    evidence_id: str | None = None


class GateResult(BaseModel):
    path: GatePath
    severity: Severity | None = None  # 1차 심각도 (05-rules §3)
    signals: list[Signal] = []  # 게이트를 연 신호
    symptoms: list[Symptom] = []  # 게이트를 연 증상 (경로 B)
    reason: str = ""
