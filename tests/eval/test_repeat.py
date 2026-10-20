"""반복 점검(check_repeatedly) 단위 테스트. 클러스터 없이 가짜 점검·시계로 돈다."""

from datetime import timedelta
from types import SimpleNamespace

from core.db import create_db_engine
from run import check_repeatedly
from service.model import Signal
from service.pipeline.check import run_check
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster


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


def _real_checks(engine_for_each_check):
    """실제 run_check + SQLite. 점검 시각(now)은 가짜 시계를 따른다 (S02 15분 지속 규칙)."""
    clock = FakeClock([None], start_s=0.0, check_s=0.0)

    def check():
        clock.calls += 1
        return run_check(
            FixtureReader(*raw_cluster("s02/partial")),
            CONFIG,
            engine_for_each_check(),
            trigger="check",
            now=NOW + timedelta(seconds=clock.now),
        )

    return check_repeatedly(
        check,
        {"expected_signals": ["S02"], "gate_path": "A"},
        interval_s=300,
        max_s=2700,
        elapsed=clock.elapsed,
        sleep=clock.sleep,
    )


def test_same_db_lets_hold_rule_mature(tmp_path):
    """같은 DB면 앞 점검의 관찰 기록이 남아 15분 뒤 점검에서 S02가 observing → detected."""
    engine = create_db_engine(tmp_path / "kg.db")
    outcome, checks, first = _real_checks(lambda: engine)

    assert [c["observing"] for c in checks[:3]] == [["S02"]] * 3
    assert checks[-1]["detected"] == ["S02"] and checks[-1]["elapsed_s"] == 900
    assert outcome.gate.path == "A"
    assert first == {"S02": 900}


def test_fresh_db_each_check_never_matures(tmp_path):
    """점검마다 새 DB면 관찰 기록이 없어 상한까지 observing으로 남는다 (예전 러너의 문제)."""
    counter = iter(range(100))
    outcome, checks, first = _real_checks(
        lambda: create_db_engine(tmp_path / f"kg-{next(counter)}.db")
    )

    assert all(c["observing"] == ["S02"] and c["detected"] == [] for c in checks)
    assert len(checks) == 10  # 0, 300, ..., 2700초
    assert first == {}


def test_collect_failure_stops_loop():
    """수집 실패는 호출한 쪽이 무효로 처리한다. 더 기다리지 않는다."""
    clock = FakeClock([_outcome(observing=["W02"], failed=["pods"])])
    (outcome, checks, first), _ = _run(clock, {"expected_signals": ["W02"], "gate_path": "A"})

    assert clock.calls == 1
    assert checks[0]["collect_failed"] == ["pods"]
