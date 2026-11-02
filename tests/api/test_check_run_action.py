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


def test_manual_executes_reserved_run_and_queues_new_incident(engine):
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
    [incident] = IncidentStore(engine).open_incidents()
    assert incident.investigation == "pending"
    from core.db import AlertStore, InvestigationQueue

    assert [a.kind for a in AlertStore(engine).for_incident(incident.id)] == ["M1"]
    job = InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL)
    assert job.check_run_id == run and job.request["trigger"] == "ui"
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


def _manual(engine, now, fixture="s04/localhost", sender_factory=None):
    from service.notify.sender import DryRunSender
    from tests.service.checks.detectors.cases import CONFIG, FixtureReader, raw_cluster

    tasks = []
    run = start_manual_check(
        engine,
        CONFIG,
        now,
        tasks.append,
        reader_factory=lambda _: FixtureReader(*raw_cluster(fixture)),
        http_factory=lambda _: None,
        sender_factory=sender_factory or (lambda _: DryRunSender()),
    )
    tasks[0]()
    return run


@pytest.mark.parametrize("failed", [False, True])
def test_manual_check_worker_result_links_to_history_issue_and_alerts(client, engine, failed):
    from core.db import AlertStore, IncidentStore, InvestigationStore
    from tests.service.checks.detectors.cases import NOW
    from tests.service.pipeline.test_worker import _worker
    from tests.test_cli import _scripted

    run = _manual(engine, NOW)
    [incident] = IncidentStore(engine).open_incidents()
    queued = client.get(f"/dev/checks/{run}").text
    assert "대기 중" in queued and f"/dev/issues/{incident.id}" in queued

    def make_llm(_):
        if failed:
            raise RuntimeError("LLM unavailable")
        return llm

    llm = _scripted()
    assert _worker(engine, make_llm).run_once() == incident.id
    stored = InvestigationStore(engine).for_check(run)
    assert len(stored) == 1 and stored[0].incident_id == incident.id
    assert stored[0].status == ("failed" if failed else "done")
    assert IncidentStore(engine).get(incident.id).investigation == stored[0].status
    assert [a.kind for a in AlertStore(engine).for_incident(incident.id)] == ["M1", "M1u", "M9"]
    text = client.get(f"/dev/issues/{incident.id}").text
    assert ("조사 실패" if failed else "gateway 주소 env가 localhost") in text
    assert CheckResultStore(engine).get(run).status == "done"


def test_manual_then_schedule_same_fault_does_not_duplicate_job_or_first_alert(engine):
    from datetime import timedelta

    from core.db import AlertStore, IncidentStore, InvestigationQueue
    from service.notify.notifier import Notifier
    from service.notify.sender import DryRunSender
    from service.pipeline.check import run_check
    from service.pipeline.investigate import queue_investigations
    from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster

    first = _manual(engine, NOW)
    _manual(engine, NOW + timedelta(seconds=61))
    later = run_check(
        FixtureReader(*raw_cluster("s04/localhost")),
        CONFIG,
        engine,
        trigger="schedule",
        now=NOW + timedelta(seconds=62),
    )
    Notifier(engine, CONFIG, DryRunSender()).after_check(
        later.run_id, later.gate, later.incidents, NOW
    )
    queue_investigations(engine, CONFIG, later, trigger="schedule", now=NOW)
    [incident] = IncidentStore(engine).open_incidents()
    assert [a.kind for a in AlertStore(engine).for_incident(incident.id)] == ["M1"]
    job = InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL)
    assert job.check_run_id == first
    assert InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL) is None


@pytest.mark.parametrize("stage", ["sender", "notify"])
def test_notification_failure_does_not_prevent_manual_investigation(engine, monkeypatch, stage):
    from core.db import InvestigationQueue
    from service.notify.notifier import Notifier
    from tests.service.checks.detectors.cases import NOW

    def broken(*args, **kwargs):
        raise RuntimeError("private diagnostic value")

    if stage == "notify":
        monkeypatch.setattr(Notifier, "after_check", broken)
    run = _manual(engine, NOW, sender_factory=broken if stage == "sender" else None)
    assert CheckResultStore(engine).get(run).status == "done"
    assert InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL).check_run_id == run
    assert CheckLockStore(engine).current(NOW) is None


def test_manual_recovery_sends_m3_once_and_clears_pending_job(engine):
    from datetime import timedelta

    from core.db import AlertStore, IncidentStore, InvestigationQueue
    from tests.service.checks.detectors.cases import NOW

    _manual(engine, NOW)
    run = _manual(engine, NOW + timedelta(minutes=2), fixture=None)
    _manual(engine, NOW + timedelta(minutes=4), fixture=None)
    incident = IncidentStore(engine).get(1)
    assert incident.status == "resolved"
    assert run in IncidentStore(engine).check_ids(1)
    assert [a.kind for a in AlertStore(engine).for_incident(1)] == ["M1", "M3"]
    assert InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL) is None


@pytest.mark.parametrize("unknown", [False, True])
def test_manual_normal_or_unknown_does_not_open_or_investigate(engine, unknown):
    from core.db import AlertStore, IncidentStore, InvestigationQueue
    from service.notify.sender import DryRunSender
    from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster

    objects, _ = raw_cluster()
    tasks = []
    run = start_manual_check(
        engine,
        CONFIG,
        NOW,
        tasks.append,
        reader_factory=lambda _: FixtureReader(
            objects, {"Service": "unavailable"} if unknown else {}
        ),
        http_factory=lambda _: None,
        sender_factory=lambda _: DryRunSender(),
    )
    tasks[0]()
    assert CheckResultStore(engine).get(run).gate_path == "C"
    assert IncidentStore(engine).open_incidents() == []
    assert InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL) is None
    assert AlertStore(engine).for_incident(1) == []


def test_manual_new_alert_keeps_ack_button_when_listener_configured(engine, monkeypatch):
    from service.notify.sender import DryRunSender
    from tests.service.checks.detectors.cases import NOW

    class FakeLiveSender(DryRunSender):
        dry_run = False
        blocks = None

        def post(self, channel, text, **kwargs):
            self.blocks = kwargs.get("blocks")
            return "123.001"

    monkeypatch.setenv("SLACK_APP_TOKEN", "configured-test-token")
    sender = FakeLiveSender()
    _manual(engine, NOW, sender_factory=lambda _: sender)
    import json

    assert "kg_ack" in json.dumps(sender.blocks)


def test_manual_fingerprint_change_replaces_pending_request(engine):
    from datetime import timedelta

    from core.db import AlertStore, IncidentStore, InvestigationQueue
    from tests.service.checks.detectors.cases import NOW

    _manual(engine, NOW)
    latest = _manual(engine, NOW + timedelta(minutes=2), fixture="s01/detected")
    [incident] = IncidentStore(engine).open_incidents()
    job = InvestigationQueue(engine).claim(NOW, CHECK_LOCK_TTL)
    assert job.check_run_id == latest and job.incident_id == incident.id
    assert job.request["trigger"] == "ui"
    assert sum(a.kind == "M1" for a in AlertStore(engine).for_incident(incident.id)) == 1
