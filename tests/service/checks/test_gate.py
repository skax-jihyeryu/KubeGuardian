from service.checks.gate import decide_gate
from service.checks.signal_board import build_signal_board
from service.model import Signal, Symptom


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


def _sym(name="chat", failed=1, severity="critical"):
    return Symptom(
        scenario=name,
        status="failed" if failed else "passed",
        failed_step=failed,
        summary=f"시나리오 {name}",
        severity=severity if failed else None,
    )


def test_failed_symptom_without_signals_opens_path_b():
    gate = decide_gate([_s("S01", verdict="passed")], [_sym("chat", 2)])
    assert gate.path == "B"
    assert gate.severity == "critical"
    assert [x.scenario for x in gate.symptoms] == ["chat"]
    assert gate.signals == []
    assert "chat" in gate.reason and "2단계" in gate.reason


def test_path_b_severity_is_highest_failed_symptom():
    gate = decide_gate([], [_sym("a", 1, "warning"), _sym("b", 3, "critical")])
    assert gate.severity == "critical"
    assert [x.scenario for x in gate.symptoms] == ["a", "b"]


def test_passing_symptoms_do_not_open_the_gate():
    gate = decide_gate([_s("H01", severity="info")], [_sym("chat", None)])
    assert (gate.path, gate.symptoms) == ("C", [])
    assert "개선 권고 1건" in gate.reason


def _unknown(name="chat"):
    return Symptom(scenario=name, status="unknown", summary="조회 실패: Service gateway 없음")


def test_unknown_scenario_does_not_open_the_gate_but_is_reported():
    gate = decide_gate([_s("S01", verdict="passed")], [_unknown("chat"), _unknown("export")])
    assert (gate.path, gate.symptoms) == ("C", [])
    assert "증상 감지 확인하지 못함 2건" in gate.reason


def test_unknown_scenario_next_to_failed_one_still_opens_path_b():
    gate = decide_gate([], [_unknown("a"), _sym("b", 2)])
    assert gate.path == "B" and [x.scenario for x in gate.symptoms] == ["b"]


def test_signal_keeps_path_a_and_severity_is_max_of_signal_and_symptom():
    gate = decide_gate([_s("S01", severity="warning")], [_sym("chat", 1, "critical")])
    assert (gate.path, gate.severity) == ("A", "critical")
    assert [x.scenario for x in gate.symptoms] == ["chat"]
    assert [s.rule_id for s in gate.signals] == ["S01"]


def test_path_a_keeps_signal_severity_when_symptom_is_milder():
    gate = decide_gate([_s("S01", severity="critical")], [_sym("export", 1, "warning")])
    assert (gate.path, gate.severity) == ("A", "critical")
    assert [x.scenario for x in gate.symptoms] == ["export"]


def test_path_a_ignores_passed_and_unknown_symptoms():
    gate = decide_gate([_s("S01", severity="warning")], [_sym("a", None), _unknown("b")])
    assert (gate.path, gate.severity, gate.symptoms) == ("A", "warning", [])


def test_user_symptom_alone_opens_path_b_as_warning():
    gate = decide_gate([], user_symptom="채팅이 안 돼요")
    assert (gate.path, gate.severity) == ("B", "warning")
    assert "사용자 증상" in gate.reason


def test_user_symptom_with_failed_scenario_takes_scenario_severity():
    gate = decide_gate([], [_sym("chat", 1, "critical")], user_symptom="안 돼요")
    assert (gate.path, gate.severity) == ("B", "critical")
    assert "chat" in gate.reason and "사용자 증상" in gate.reason


def test_user_symptom_with_signal_stays_path_a():
    gate = decide_gate([_s("S01", severity="critical")], user_symptom="안 돼요")
    assert gate.path == "A"
