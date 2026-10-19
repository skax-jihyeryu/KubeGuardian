"""시나리오 채점: 기준 조건(규칙만). AI 조사 결과 채점은 W2에 붙인다.

통과 = 기대 장애 신호를 모두 적발 + 게이트 경로가 기대와 같음.
기대 밖 적발(unexpected)은 오탐 후보로 기록만 하고 통과 여부는 게이트 경로가 정한다.
"""

from typing import Any

from service.checks.detectors.catalog import CATALOG
from service.model import GatePath, Signal


def _rules(signals: list[Signal], verdict: str, grade: str) -> set[str]:
    return {
        s.rule_id for s in signals if s.verdict == verdict and CATALOG[s.rule_id].grade == grade
    }


def score(expected: dict[str, Any], signals: list[Signal], gate_path: GatePath) -> dict[str, Any]:
    want = set(expected.get("expected_signals") or [])
    detected = _rules(signals, "detected", "signal")
    gate_ok = gate_path == expected["gate_path"]
    missed = want - detected
    return {
        "passed": not missed and gate_ok,
        "gate_path": gate_path,
        "gate_expected": expected["gate_path"],
        "gate_ok": gate_ok,
        "expected": sorted(want),
        "detected": sorted(detected),
        "missed": sorted(missed),
        "unexpected": sorted(detected - want),
        "observing": sorted(_rules(signals, "observing", "signal")),
        "unknown": sorted({s.rule_id for s in signals if s.verdict == "unknown"}),
        "advice": sorted(_rules(signals, "detected", "advice")),
    }
