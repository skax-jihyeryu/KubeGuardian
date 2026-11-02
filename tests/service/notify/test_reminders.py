from datetime import timedelta

import pytest

from service.notify.notifier import Notifier, parse_duration
from tests.service.checks.detectors.cases import NOW
from tests.service.notify.test_notifier import CONFIG, _sig, env  # noqa: F401  (fixture)


def _reminders(sender):
    return [p for p in sender.posts if p["text"].startswith("⏰")]


def _remind(engine, sender, minutes, *, ack_enabled=True):
    """작업자가 NOW + minutes에 리마인드를 판단한다."""
    Notifier(engine, CONFIG, sender, ack_enabled=ack_enabled).check_reminders(
        NOW + timedelta(minutes=minutes)
    )


def test_critical_reminds_every_30m_from_last_reminder(env):  # noqa: F811
    engine, sender, check = env
    check([_sig()], NOW)

    sent_at = {}
    for minutes in (5, 29, 30, 59, 60, 61, 89, 90):
        before = len(_reminders(sender))
        _remind(engine, sender, minutes)
        if len(_reminders(sender)) > before:
            sent_at[minutes] = _reminders(sender)[-1]["text"]

    assert list(sent_at) == [30, 60, 90]  # 30분 뒤 첫 리마인드, 이후 마지막 리마인드에서 30분마다
    assert "30분째" in sent_at[30] and all(p["thread"] == "ts-1" for p in _reminders(sender))


def test_stopped_worker_reminds_once_then_resumes_schedule(env):  # noqa: F811
    engine, sender, check = env
    check([_sig()], NOW)
    _remind(engine, sender, 30)

    _remind(engine, sender, 210)  # 작업자가 3시간 멈췄다 재개
    _remind(engine, sender, 211)
    _remind(engine, sender, 239)  # 마지막 리마인드(210분)에서 30분이 안 됐다
    _remind(engine, sender, 240)

    assert len(_reminders(sender)) == 3  # 30분, 210분, 240분. 밀린 만큼 연달아 나가지 않는다


def test_ack_stops_critical_reminders(env):  # noqa: F811
    from core.db import IncidentAckStore

    engine, sender, check = env
    check([_sig()], NOW)
    _remind(engine, sender, 30)
    IncidentAckStore(engine).ack(1, "U1", "홍길동", NOW + timedelta(minutes=40))

    for minutes in (60, 90, 600):
        _remind(engine, sender, minutes)

    assert len(_reminders(sender)) == 1


def test_critical_without_ack_channel_is_capped_at_three(env):  # noqa: F811
    engine, sender, check = env
    check([_sig()], NOW)

    for minutes in range(30, 600, 30):
        _remind(engine, sender, minutes, ack_enabled=False)

    assert len(_reminders(sender)) == 3  # 확인할 수단이 없으면 끝없이 울리지 않는다


def test_check_after_check_does_not_send_reminder(env):  # noqa: F811
    _, sender, check = env
    check([_sig()], NOW)

    check([_sig()], NOW + timedelta(minutes=45))  # 점검이 이어져도 리마인드는 작업자의 일이다

    assert _reminders(sender) == []


def test_warning_reminds_once_after_2h(env):  # noqa: F811
    engine, sender, check = env
    check([_sig(severity="warning")], NOW)

    for minutes in (60, 119, 121, 300, 600):
        _remind(engine, sender, minutes)

    assert len(_reminders(sender)) == 1 and "121분째" in _reminders(sender)[0]["text"]


def test_no_reminder_after_resolution(env):  # noqa: F811
    engine, sender, check = env
    check([_sig()], NOW)
    check([_sig(verdict="passed")], NOW + timedelta(minutes=10))
    _remind(engine, sender, 40)  # 해결된 이슈는 30분이 지나도 리마인드하지 않는다

    check([_sig()], NOW + timedelta(minutes=45))  # 다시 깨지면 새 이슈(M1)지 리마인더가 아니다
    _remind(engine, sender, 50)

    assert _reminders(sender) == []
    assert sum(p["thread"] is None for p in sender.posts) == 2


def test_policy_missing_means_no_reminder(tmp_path):
    from core.db import create_db_engine
    from service.alerts.incidents import track
    from service.checks.gate import decide_gate
    from service.notify.notifier import Notifier
    from tests.service.notify.test_notifier import FakeSender

    config = CONFIG.model_copy(update={"slack": CONFIG.slack.model_copy(update={"reminders": {}})})
    engine, sender = create_db_engine(tmp_path / "kg.db"), FakeSender()
    for n, minutes in enumerate((0, 40, 200), start=1):
        at, sig = NOW + timedelta(minutes=minutes), _sig()
        gate = decide_gate([sig])
        changes = track(
            engine,
            config,
            run_id=n,
            trigger="schedule",
            now=at,
            gate=gate,
            signals=[sig],
            symptoms=[],
        )
        notifier = Notifier(engine, config, sender, ack_enabled=True)
        notifier.after_check(n, gate, changes, at)
        notifier.check_reminders(at)

    assert _reminders(sender) == []


@pytest.mark.parametrize(
    ("text", "td"), [("30m", timedelta(minutes=30)), ("2h", timedelta(hours=2))]
)
def test_parse_duration(text, td):
    assert parse_duration(text) == td
