"""AI 게이트 v0: 경로 A·C (기획서 §3.1·§6.2, 05-rules §0.1·§3).

- A: 장애 신호(N·P·W·S·C) 규칙이 하나라도 적발 → 1차 심각도는 적발된 신호 중 가장 높은 것
- C: 그 밖 (개선 권고만, 관찰 중, 확인하지 못함, 모두 통과)
- B(규칙 통과 + 증상)는 증상 감지(W2)를 붙일 때 더한다.
"""

from collections import Counter

from service.checks.detectors.catalog import CATALOG
from service.model import GateResult, Severity, Signal

_RANK: dict[Severity, int] = {"info": 0, "warning": 1, "critical": 2}


def decide_gate(signals: list[Signal]) -> GateResult:
    opening = [
        s for s in signals if s.verdict == "detected" and CATALOG[s.rule_id].grade == "signal"
    ]
    if opening:
        severity = max((s.severity for s in opening if s.severity), key=_RANK.__getitem__)
        rules = ", ".join(dict.fromkeys(s.rule_id for s in opening))
        return GateResult(path="A", severity=severity, signals=opening, reason=f"적발: {rules}")
    return GateResult(path="C", reason=_summary(signals))


def _summary(signals: list[Signal]) -> str:
    counts = Counter(
        "개선 권고"
        if s.verdict == "detected"
        else {"observing": "관찰 중", "unknown": "확인하지 못함"}[s.verdict]
        for s in signals
        if s.verdict != "passed"
    )
    if not counts:
        return "장애 신호 없음"
    return "장애 신호 없음 (" + ", ".join(f"{k} {v}건" for k, v in counts.items()) + ")"
