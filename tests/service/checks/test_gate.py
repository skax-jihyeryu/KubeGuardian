from service.checks.gate import decide_gate
from service.checks.signal_board import build_signal_board
from service.model import Signal


def _s(rule_id, verdict="detected", severity="warning", service="agent", subject=None):
    return Signal(
        rule_id=rule_id,
        verdict=verdict,
        subject=subject or f"dev/Service/{service}",
        service=service,
        severity=severity if verdict == "detected" else None,
    )


def test_detected_failure_signal_opens_path_a_with_highest_severity():
    signals = [
        _s("S02", severity="warning"),
        _s("S01", severity="critical"),
        _s("H01", severity="info"),
    ]
    gate = decide_gate(signals)
    assert gate.path == "A"
    assert gate.severity == "critical"
    assert [s.rule_id for s in gate.signals] == ["S02", "S01"]  # 개선 권고는 게이트를 열지 않음


def test_advice_only_is_path_c():
    gate = decide_gate([_s("H01", severity="info"), _s("H02", severity="info")])
    assert (gate.path, gate.severity, gate.signals) == ("C", None, [])
    assert "개선 권고 2건" in gate.reason


def test_observing_and_unknown_do_not_open_the_gate():
    gate = decide_gate([_s("S02", verdict="observing"), _s("S04", verdict="unknown")])
    assert gate.path == "C"
    assert "관찰 중 1건" in gate.reason
    assert "확인하지 못함 1건" in gate.reason


def test_nothing_is_path_c():
    assert decide_gate([_s("S01", verdict="passed")]).path == "C"


def test_signal_board_groups_non_passed_signals_by_service():
    board = build_signal_board(
        [
            _s("S01", service="agent"),
            _s("S02", verdict="observing", service="agent"),
            _s("S04", service="gateway"),
            _s("S03", verdict="passed", service="gateway"),
            Signal(rule_id="N01", verdict="detected", subject="Node/minikube", severity="critical"),
        ]
    )
    assert {k: [s.rule_id for s in v] for k, v in board.by_service.items()} == {
        "agent": ["S01", "S02"],
        "gateway": ["S04"],
        "Node/minikube": ["N01"],  # 서비스가 없는 신호는 대상으로 묶는다
    }
