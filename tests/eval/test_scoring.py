from scoring import expected_record, score
from service.model import Signal


def _sig(rule_id, verdict="detected", severity="critical"):
    return Signal(
        rule_id=rule_id,
        verdict=verdict,
        subject=f"dev/Service/{rule_id.lower()}",
        severity=severity if verdict == "detected" else None,
    )


def test_expected_signal_detected_and_gate_matches_passes():
    result = score({"must_signals": ["S04"], "gate_path": "A"}, [_sig("S04")], "A")

    assert result["passed"] is True
    assert result["detected"] == ["S04"]
    assert result["missed"] == []
    assert result["unexpected"] == []


def test_missed_signal_fails():
    result = score(
        {"must_signals": ["S01"], "gate_path": "A"}, [_sig("S01", verdict="observing")], "C"
    )

    assert result["passed"] is False
    assert result["missed"] == ["S01"]
    assert result["observing"] == ["S01"]
    assert result["gate_ok"] is False


def test_advice_rules_are_not_counted_as_signals():
    """H0: 개선 권고(H01)만 나오면 장애 신호 없음 = 통과."""
    result = score({"must_signals": [], "gate_path": "C"}, [_sig("H01", severity="info")], "C")

    assert result["passed"] is True
    assert result["detected"] == []
    assert result["advice"] == ["H01"]


def test_unexpected_signal_is_reported_but_gate_decides():
    result = score({"must_signals": ["S04"], "gate_path": "A"}, [_sig("S04"), _sig("P06")], "A")

    assert result["passed"] is True
    assert result["unexpected"] == ["P06"]


def test_unknown_rules_are_listed():
    result = score({"must_signals": [], "gate_path": "C"}, [_sig("S02", verdict="unknown")], "C")

    assert result["unknown"] == ["S02"]


def test_forbidden_signal_fails():
    exp = {"must_signals": [], "forbidden_signals": ["S04"], "gate_path": "A"}
    result = score(exp, [_sig("S04")], "A")

    assert result["forbidden_hit"] == ["S04"]
    assert result["passed"] is False
    assert result["unexpected"] == []


def test_forbidden_star_means_all_signals():
    exp = {"must_signals": [], "forbidden_signals": ["*"], "gate_path": "C"}
    result = score(exp, [_sig("S04"), _sig("C05")], "C")

    assert result["forbidden_hit"] == ["C05", "S04"]
    assert result["passed"] is False
    assert score(exp, [], "C")["passed"] is True


def test_forbidden_star_excludes_must_and_allowed_signals():
    exp = {
        "must_signals": ["S04"],
        "allowed_signals": ["P03"],
        "forbidden_signals": ["*"],
        "gate_path": "A",
    }
    result = score(exp, [_sig("S04"), _sig("P03"), _sig("C05")], "A")

    assert result["forbidden_hit"] == ["C05"]
    assert result["passed"] is False
    assert score(exp, [_sig("S04"), _sig("P03")], "A")["passed"] is True


def test_allowed_signal_not_unexpected():
    exp = {"must_signals": ["W02"], "allowed_signals": ["P03"], "gate_path": "A"}
    result = score(exp, [_sig("W02"), _sig("P03"), _sig("P06")], "A")

    assert result["passed"] is True
    assert result["unexpected"] == ["P06"]


def test_unexpected_still_recorded_only():
    exp = {"must_signals": ["S04"], "forbidden_signals": [], "gate_path": "A"}
    result = score(exp, [_sig("S04"), _sig("P06")], "A")

    assert result["passed"] is True
    assert result["unexpected"] == ["P06"]
    assert result["forbidden_hit"] == []


def test_advice_rules_never_forbidden():
    exp = {"must_signals": [], "forbidden_signals": ["*"], "gate_path": "C"}
    result = score(exp, [_sig("H01", severity="info")], "C")

    assert result["forbidden_hit"] == []
    assert result["passed"] is True


def test_must_check_and_not_a_cause_are_recorded_not_scored():
    exp = {
        "must_signals": [],
        "gate_path": "C",
        "must_check": {"config": "env 값"},
        "not_a_cause": ["개선 권고"],
    }

    assert expected_record(exp) == {
        "must_check": {"config": "env 값"},
        "not_a_cause": ["개선 권고"],
    }
    assert expected_record({"gate_path": "C"}) == {"must_check": {}, "not_a_cause": []}
