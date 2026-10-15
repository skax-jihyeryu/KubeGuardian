"""규칙 판정 결과와 신호판 (05-rules §0.1, 상세설계 §2.1)."""

from typing import Any, Literal

from pydantic import BaseModel

Verdict = Literal[
    "detected", "observing", "passed", "unknown"
]  # 적발 / 관찰 중 / 통과 / 확인하지 못함
Severity = Literal["critical", "warning", "info"]  # 긴급 / 주의 / 참고


class Signal(BaseModel):
    rule_id: str  # 규칙 목록의 ID (S04 등)
    verdict: Verdict
    subject: str  # 판정 대상 리소스 (dev/Service/agent)
    service: str | None = None  # 묶을 서비스 이름
    severity: Severity | None = None  # 1차 심각도 (적발일 때)
    values: dict[str, Any] = {}  # 판정에 쓴 값 (P4 표시, 근거)
    message: str = ""
    evidence_id: str | None = None


class SignalBoard(BaseModel):
    """원시 신호를 서비스별로 묶은 것. 에이전트 입력이다."""

    by_service: dict[str, list[Signal]] = {}
