"""반복 점검(check_repeatedly) 단위 테스트. 클러스터 없이 가짜 점검·시계로 돈다."""

from types import SimpleNamespace

from run import check_repeatedly
from service.model import Signal


def _outcome(detected=(), observing=(), gate="C", failed=()):
    signals = [
        Signal(rule_id=r, verdict="detected", subject=f"dev/{r}", severity="critical")
        for r in detected
    ] + [Signal(rule_id=r, verdict="observing", subject=f"dev/{r}") for r in observing]
    return SimpleNamespace(
        run_id=1,
        signals=signals,
        gate=SimpleNamespace(path=gate, reason="test"),
        snapshot=SimpleNamespace(failed=list(failed)),
    )


class FakeClock:
    """점검 한 번에 check_s초가 걸리고, sleep은 시간을 그만큼 넘긴다."""

    def __init__(self, outcomes, start_s=30.0, check_s=10.0):
        self.now = start_s
        self.outcomes = list(outcomes)
        self.check_s = check_s
        self.sleeps = []
        self.calls = 0

    def check(self):
        self.calls += 1
        self.now += self.check_s
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def elapsed(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def _run(clock, expected, interval_s=300, max_s=2100):
    printed = []
    result = check_repeatedly(
        clock.check,
        expected,
        interval_s=interval_s,
        max_s=max_s,
        elapsed=clock.elapsed,
        sleep=clock.sleep,
        on_check=printed.append,
    )
    return result, printed


def test_no_expected_signals_checks_once():
    """H0·B·C 시나리오: 기대 신호가 없으면 한 번만 점검한다."""
    clock = FakeClock([_outcome(gate="C")])
    (outcome, checks, first), printed = _run(clock, {"expected_signals": [], "gate_path": "C"})

    assert clock.calls == 1
    assert clock.sleeps == []
    assert len(checks) == 1 and printed == checks
    assert checks[0]["gate_path"] == "C"
    assert first == {}


def test_immediate_signal_finishes_after_first_check():
    clock = FakeClock([_outcome(detected=["S01"], gate="A")])
    (outcome, checks, first), _ = _run(clock, {"expected_signals": ["S01"], "gate_path": "A"})

    assert clock.calls == 1
    assert first == {"S01": 40}
    assert checks[0]["detected"] == ["S01"]


def test_repeats_until_all_expected_detected_and_records_first_detection():
    """A3: S02는 2번째 점검, W02는 6번째 점검에서 적발. 같은 결과를 마지막 점검으로 쓴다."""
    outcomes = [
        _outcome(observing=["S02", "W02"]),
        _outcome(detected=["S02"], observing=["W02"], gate="A"),
        *[_outcome(detected=["S02"], observing=["W02"], gate="A")] * 3,
        _outcome(detected=["S02", "W02"], gate="A"),
    ]
    clock = FakeClock(outcomes)
    (outcome, checks, first), _ = _run(clock, {"expected_signals": ["W02"], "gate_path": "A"})

    assert clock.calls == 6
    assert outcome is outcomes[-1]
    assert [c["detected"] for c in checks][-1] == ["S02", "W02"]
    assert first == {"W02": checks[-1]["elapsed_s"]}
    # 첫 점검 시작(30초) 기준 5분 간격 일정: 30, 330, 630, ... (점검 시간만큼 sleep이 줄어든다)
    assert clock.sleeps == [290.0] * 5
    assert [c["elapsed_s"] for c in checks] == [40, 340, 640, 940, 1240, 1540]


def test_first_detection_is_kept_and_stop_needs_all_in_one_check():
    """채점은 마지막 점검으로 하므로 한 점검에서 모두 적발되어야 멈춘다.

    첫 적발 시각은 처음 것을 남긴다."""
    outcomes = [
        _outcome(detected=["S02"], observing=["W02"]),
        _outcome(observing=["W02"]),
        _outcome(detected=["W02"], gate="A"),
        _outcome(detected=["S02", "W02"], gate="A"),
    ]
    clock = FakeClock(outcomes)
    (outcome, checks, first), _ = _run(
        clock, {"expected_signals": ["S02", "W02"], "gate_path": "A"}
    )

    assert clock.calls == 4
    assert first == {"S02": 40, "W02": 640}


def test_stops_at_cap_and_returns_last_outcome():
    clock = FakeClock([_outcome(observing=["W02"])])
    (outcome, checks, first), _ = _run(
        clock, {"expected_signals": ["W02"], "gate_path": "A"}, interval_s=300, max_s=900
    )

    # 30초에 첫 점검, 330·630초까지. 다음(930초)은 상한 900초를 넘어 하지 않는다
    assert clock.calls == 3
    assert first == {}
    assert checks[-1]["gate_path"] == "C"


def test_collect_failure_stops_loop():
    """수집 실패는 호출한 쪽이 무효로 처리한다. 더 기다리지 않는다."""
    clock = FakeClock([_outcome(observing=["W02"], failed=["pods"])])
    (outcome, checks, first), _ = _run(clock, {"expected_signals": ["W02"], "gate_path": "A"})

    assert clock.calls == 1
    assert checks[0]["collect_failed"] == ["pods"]
