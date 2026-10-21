"""AI 게이트 v1: 경로 A·B·C (기획서 §3.1·§6.2, 05-rules §0.1·§3).

- A: 장애 신호(N·P·W·S·C) 규칙이 하나라도 적발 → 1차 심각도는 적발된 신호 중 가장 높은 것.
  사용자 증상이 있어도 A다 (사용자 증상은 조사 요청에 그대로 전달한다).
- B: 신호는 없는데 실패한 증상(API 시나리오)이나 사용자 증상이 있음
  → 심각도는 실패한 증상 중 가장 높은 것, 사용자 증상만 있으면 warning
- C: 그 밖 (개선 권고만, 관찰 중, 확인하지 못함, 모두 통과)
"""

from collections import Counter
from collections.abc import Sequence

from service.checks.detectors.catalog import CATALOG
from service.model import GateResult, Severity, Signal, Symptom

_RANK: dict[Severity, int] = {"info": 0, "warning": 1, "critical": 2}


def decide_gate(
    signals: list[Signal],
    symptoms: Sequence[Symptom] = (),
    user_symptom: str | None = None,
) -> GateResult:
    opening = [
        s for s in signals if s.verdict == "detected" and CATALOG[s.rule_id].grade == "signal"
    ]
    if opening:
        severity = max((s.severity for s in opening if s.severity), key=_RANK.__getitem__)
        rules = ", ".join(dict.fromkeys(s.rule_id for s in opening))
        return GateResult(path="A", severity=severity, signals=opening, reason=f"적발: {rules}")
    failed = [x for x in symptoms if x.failed_step is not None]
    if failed or user_symptom:
        severity = max(
            (x.severity for x in failed if x.severity), key=_RANK.__getitem__, default="warning"
        )
        parts = [f"{x.scenario} {x.failed_step}단계 실패" for x in failed]
        if user_symptom:
            parts.append("사용자 증상")
        return GateResult(
            path="B", severity=severity, symptoms=failed, reason="증상: " + ", ".join(parts)
        )
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
