from datetime import timedelta

from core.config import KubeGuardianConfig
from core.db import CheckRunStore
from deps import get_config
from service.notify.notifier import Notifier
from tests.api.seed import at
from tests.service.notify.test_notifier import FakeSender
from web.viewmodels.shell import build_shell


def hourly():
    return KubeGuardianConfig(
        project={"id": "dev"}, target={"namespace": "dev"}, schedule="0 * * * *"
    )


def test_hourly_shell_shows_next_hour_without_false_stall(engine):
    CheckRunStore(engine).create("schedule", at(0))
    shell = build_shell(engine, hourly(), at(0) + timedelta(minutes=40))
    assert shell.next_time == "15:00"
    assert shell.halted_minutes is None


def test_hourly_notifier_waits_for_three_missed_checks(engine):
    CheckRunStore(engine).create("schedule", at(0))
    notifier = Notifier(engine, hourly(), FakeSender())
    assert not notifier.check_stalled(at(0) + timedelta(minutes=61))
    assert notifier.check_stalled(at(0) + timedelta(minutes=181))


def test_hourly_checks_do_not_create_false_gap_rows(client, engine):
    client.app.dependency_overrides[get_config] = hourly
    CheckRunStore(engine).create("schedule", at(0))
    CheckRunStore(engine).create("schedule", at(3600))
    assert "점검 중단 ·" not in client.get("/dev/checks").text
