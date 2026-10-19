from scoring import score
from service.model import Signal


def _sig(rule_id, verdict="detected", severity="critical"):
    return Signal(
        rule_id=rule_id,
        verdict=verdict,
        subject=f"dev/Service/{rule_id.lower()}",
        severity=severity if verdict == "detected" else None,
    )


def test_expected_signal_detected_and_gate_matches_passes():
    result = score({"expected_signals": ["S04"], "gate_path": "A"}, [_sig("S04")], "A")

    assert result["passed"] is True
    assert result["detected"] == ["S04"]
    assert result["missed"] == []
    assert result["unexpected"] == []


def test_missed_signal_fails():
    result = score(
        {"expected_signals": ["S01"], "gate_path": "A"}, [_sig("S01", verdict="observing")], "C"
    )

    assert result["passed"] is False
    assert result["missed"] == ["S01"]
    assert result["observing"] == ["S01"]
    assert result["gate_ok"] is False


def test_advice_rules_are_not_counted_as_signals():
    """H0: 개선 권고(H01)만 나오면 장애 신호 없음 = 통과."""
    result = score({"expected_signals": [], "gate_path": "C"}, [_sig("H01", severity="info")], "C")

    assert result["passed"] is True
    assert result["detected"] == []
    assert result["advice"] == ["H01"]


def test_unexpected_signal_is_reported_but_gate_decides():
    result = score({"expected_signals": ["S04"], "gate_path": "A"}, [_sig("S04"), _sig("P06")], "A")

    assert result["passed"] is True
    assert result["unexpected"] == ["P06"]


def test_unknown_rules_are_listed():
    result = score(
        {"expected_signals": [], "gate_path": "C"}, [_sig("S02", verdict="unknown")], "C"
    )

    assert result["unknown"] == ["S02"]
