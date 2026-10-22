"""AI 게이트 판정 (기획서 §3.1·§6.2)."""

from typing import Literal

from pydantic import BaseModel

from service.model.signal import Severity, Signal

GatePath = Literal["A", "B", "C"]  # A 규칙 적발 / B 규칙 통과 + 증상 / C 정상


class Symptom(BaseModel):
    """API 시나리오 한 건의 실행 결과 (기획서 §7.3).

    failed: 시나리오가 돌았고 단계가 실패 = 증상. passed: 모두 성공.
    unknown: 시나리오를 실행하지 못함 (계정 없음, Service·포트 못 읽음 등). 증상이 아니다.
    """

    scenario: str
    status: Literal["passed", "failed", "unknown"]
    failed_step: int | None = None  # 처음 실패한 단계(1부터). 모두 성공이면 None
    summary: str  # 마스킹된 한 줄 요약. unknown이면 실행하지 못한 사유
    severity: Severity | None = None  # 실패일 때만: core_paths 시나리오 critical, 그 밖 warning
    evidence_id: str | None = None


class GateResult(BaseModel):
    path: GatePath
    severity: Severity | None = None  # 1차 심각도 (05-rules §3)
    signals: list[Signal] = []  # 게이트를 연 신호
    symptoms: list[Symptom] = []  # 실패한 증상 (경로 B는 게이트를 연 증상, 경로 A는 함께 있던 증상)
    reason: str = ""
