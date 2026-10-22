"""시나리오 채점: 기준 조건(규칙만). AI 조사 결과 채점은 W2에 붙인다.

정답 파일의 신호 구분 (05-rules §0.1.1):
- must_signals: 반드시 적발되어야 하는 장애 신호
- allowed_signals: 나와도 되는 장애 신호 (예: A3의 P03)
- forbidden_signals: 나오면 오탐인 장애 신호. `"*"`는 must·allowed를 뺀 장애 신호 전부
  (개선 권고 H는 포함하지 않는다)

통과 = must를 모두 적발 + 게이트 경로가 기대와 같음 + forbidden을 하나도 적발하지 않음.
그 밖의 적발(unexpected)은 오탐 후보로 기록만 한다.
must_check·not_a_cause는 채점하지 않고 기록만 한다 (보고서 대조는 #21).
"""

from typing import Any

from service.checks.detectors.catalog import CATALOG
from service.model import GatePath, Signal


def _rules(signals: list[Signal], verdict: str, grade: str) -> set[str]:
    return {
        s.rule_id for s in signals if s.verdict == verdict and CATALOG[s.rule_id].grade == grade
    }


def expected_record(expected: dict[str, Any]) -> dict[str, Any]:
    """채점하지 않고 result.json에 그대로 남기는 정답 항목."""
    return {
        "must_check": expected.get("must_check") or {},
        "not_a_cause": expected.get("not_a_cause") or [],
    }


def score(expected: dict[str, Any], signals: list[Signal], gate_path: GatePath) -> dict[str, Any]:
    want = set(expected.get("must_signals") or [])
    detected = _rules(signals, "detected", "signal")
    allowed = set(expected.get("allowed_signals") or [])
    forbidden = set(expected.get("forbidden_signals") or [])
    forbidden_hit = detected - want - allowed if "*" in forbidden else detected & forbidden
    gate_ok = gate_path == expected["gate_path"]
    missed = want - detected
    return {
        "passed": not missed and gate_ok and not forbidden_hit,
        "gate_path": gate_path,
        "gate_expected": expected["gate_path"],
        "gate_ok": gate_ok,
        "expected": sorted(want),
        "detected": sorted(detected),
        "missed": sorted(missed),
        "forbidden_hit": sorted(forbidden_hit),
        "unexpected": sorted(detected - want - allowed - forbidden_hit),
        "observing": sorted(_rules(signals, "observing", "signal")),
        "unknown": sorted({s.rule_id for s in signals if s.verdict == "unknown"}),
        "advice": sorted(_rules(signals, "detected", "advice")),
    }
