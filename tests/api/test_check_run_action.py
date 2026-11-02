import pytest

from core.config import KubeGuardianConfig
from core.db import CheckLockStore, CheckResultStore
from core.db.check_lock import CHECK_LOCK_TTL
from service.pipeline.manual_check import ManualCheckConflict, start_manual_check
from tests.api.seed import at

CONFIG = KubeGuardianConfig(
    project={"id": "dev"}, target={"namespace": "dev"}, core_paths={"chat": ["agent"]}
)


def test_manual_reserves_run_and_lock_before_background(engine):
    tasks = []
    run = start_manual_check(engine, CONFIG, at(0), tasks.append)
    assert CheckResultStore(engine).get(run).status == "running"
    assert CheckLockStore(engine).current(at(1)).run_id == run
    with pytest.raises(ManualCheckConflict):
        start_manual_check(engine, CONFIG, at(1), tasks.append)
    assert len(tasks) == 1


def test_initialization_failure_records_failed_and_releases_lock(engine):
    tasks = []

    def broken(config):
        raise RuntimeError("secret must not leak")

    run = start_manual_check(engine, CONFIG, at(0), tasks.append, reader_factory=broken)
    tasks[0]()
    result = CheckResultStore(engine).get(run)
    assert result.status == "failed"
    assert result.error == "RuntimeError"
    assert CheckLockStore(engine).current(at(1)) is None
    with pytest.raises(ManualCheckConflict):
        start_manual_check(engine, CONFIG, at(30), tasks.append)
    start_manual_check(engine, CONFIG, at(61), tasks.append)


def test_post_running_conflict_links_existing_run(client, engine):
    lock = CheckLockStore(engine).acquire("schedule", at(0), CHECK_LOCK_TTL)
    CheckLockStore(engine).attach_run(lock.holder.token, 123)
    response = client.post("/dev/checks")
    assert response.status_code == 409
    assert "/dev/checks/123" in response.text
    assert client.post("/other/checks").status_code == 404


def test_manual_executes_existing_run_without_opening_incident(engine):
    from core.db import CheckRunStore, IncidentStore
    from service.notify.sender import DryRunSender
    from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster

    tasks = []
    run = start_manual_check(
        engine,
        CONFIG,
        NOW,
        tasks.append,
        reader_factory=lambda config: FixtureReader(*raw_cluster("s01/detected")),
        http_factory=lambda config: None,
        sender_factory=lambda config: DryRunSender(),
    )
    tasks[0]()
    assert CheckResultStore(engine).get(run).status == "done"
    assert len(CheckRunStore(engine).list_runs()) == 1
    assert IncidentStore(engine).open_incidents() == []
    assert CheckLockStore(engine).current(NOW) is None


def test_post_redirects_to_reserved_run(client, monkeypatch):
    from api.routes import checks

    calls = []

    def start(engine, config, now, submit):
        calls.append(config.target.namespace)
        return 42

    monkeypatch.setattr(checks, "start_manual_check", start)
    response = client.post("/dev/checks")
    assert response.status_code == 303
    assert response.headers["location"] == "/dev/checks/42"
    assert calls == ["dev"]


def test_race_to_lock_returns_html_conflict_with_holder_link(client, engine, monkeypatch):
    import service.pipeline.manual_check as manual
    from core.error.service_exception import ErrorCode, ServiceException

    def raced(locks, trigger, now):
        got = locks.acquire("schedule", now, CHECK_LOCK_TTL)
        locks.attach_run(got.holder.token, 777)
        raise ServiceException(ErrorCode.CHECK_ALREADY_RUNNING)

    monkeypatch.setattr(manual, "_acquire_lock", raced)
    response = client.post("/dev/checks")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")
    assert "/dev/checks/777" in response.text
