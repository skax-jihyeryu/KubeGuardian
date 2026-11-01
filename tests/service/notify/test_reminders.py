from datetime import timedelta

import pytest

from service.notify.notifier import parse_duration
from tests.service.checks.detectors.cases import NOW
from tests.service.notify.test_notifier import _sig, env  # noqa: F401  (fixture)


def _reminders(sender):
    return [p for p in sender.posts if p["text"].startswith("⏰")]


def test_critical_reminds_after_30m_then_every_2h_up_to_3(env):  # noqa: F811
    _, sender, check = env
    check([_sig()], NOW)

    sent_at = {}
    for minutes in (5, 29, 31, 60, 150, 151, 270, 271, 390, 391, 600):
        before = len(_reminders(sender))
        check([_sig()], NOW + timedelta(minutes=minutes))
        if len(_reminders(sender)) > before:
            sent_at[minutes] = _reminders(sender)[-1]["text"]

    assert list(sent_at) == [31, 150, 270]  # 30분, 이후 2시간마다(150·270분), 총 3회
    assert "31분째" in sent_at[31] and all(p["thread"] == "ts-1" for p in _reminders(sender))


def test_warning_reminds_once_after_2h(env):  # noqa: F811
    _, sender, check = env
    check([_sig(severity="warning")], NOW)

    for minutes in (60, 119, 121, 300, 600):
        check([_sig(severity="warning")], NOW + timedelta(minutes=minutes))

    assert len(_reminders(sender)) == 1 and "121분째" in _reminders(sender)[0]["text"]


def test_no_reminder_after_resolution(env):  # noqa: F811
    _, sender, check = env
    check([_sig()], NOW)
    check([_sig(verdict="passed")], NOW + timedelta(minutes=10))

    check([_sig()], NOW + timedelta(minutes=45))  # 다시 깨지면 새 이슈(M1)지 리마인더가 아니다

    assert _reminders(sender) == []
    assert sum(p["thread"] is None for p in sender.posts) == 2


def test_policy_missing_means_no_reminder(tmp_path):
    from core.db import create_db_engine
    from service.alerts.incidents import track
    from service.checks.gate import decide_gate
    from service.notify.notifier import Notifier
    from tests.service.notify.test_notifier import CONFIG, FakeSender

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
        Notifier(engine, config, sender).after_check(n, gate, changes, at)

    assert _reminders(sender) == []


@pytest.mark.parametrize(
    ("text", "td"), [("30m", timedelta(minutes=30)), ("2h", timedelta(hours=2))]
)
def test_parse_duration(text, td):
    assert parse_duration(text) == td
