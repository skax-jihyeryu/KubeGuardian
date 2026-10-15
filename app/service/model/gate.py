"""AI 게이트 판정 (기획서 §3.1·§6.2)."""

from typing import Literal

from pydantic import BaseModel

from service.model.signal import Severity, Signal

GatePath = Literal["A", "B", "C"]  # A 규칙 적발 / B 규칙 통과 + 증상 / C 정상


class GateResult(BaseModel):
    path: GatePath
    severity: Severity | None = None  # 1차 심각도 (05-rules §3)
    signals: list[Signal] = []  # 게이트를 연 신호
    reason: str = ""
