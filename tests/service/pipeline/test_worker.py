from datetime import timedelta

import pytest

from core.config import ReminderPolicy, SlackConfig
from core.db import AlertStore, CheckRunStore, IncidentStore, InvestigationQueue, create_db_engine
from service.notify.notifier import Notifier
from service.notify.sender import DryRunSender
from service.pipeline.check import run_check
from service.pipeline.investigate import queue_investigations
from service.pipeline.worker import Worker, complete_job
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster
from tests.test_cli import _scripted


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _opened(engine, at=NOW):
    """주기 점검이 S04 이슈를 열고 1차 알림을 보낸 상태."""
    out = run_check(
        FixtureReader(*raw_cluster("s04/localhost")), CONFIG, engine, trigger="schedule", now=at
    )
    Notifier(engine, CONFIG, DryRunSender()).after_check(out.run_id, out.gate, out.incidents, at)
    queue_investigations(engine, CONFIG, out, trigger="schedule", now=at)  # 1차 알림 뒤에
    return out.incidents[0].incident_id


def _worker(engine, llm_factory, at=NOW + timedelta(seconds=5)):
    reader = FixtureReader(*raw_cluster("s04/localhost"))
    notifier = Notifier(engine, CONFIG, DryRunSender())
    return Worker(
        engine, CONFIG, notifier, llm_factory=llm_factory, make_k8s=lambda: reader, clock=lambda: at
    )


def test_worker_investigates_queued_incident_and_updates_alert(engine):
    iid = _opened(engine)
    llm = _scripted()

    assert _worker(engine, lambda model: llm).run_once() == iid

    incident = IncidentStore(engine).get(iid)
    assert (incident.investigation, incident.final_severity) == ("done", "core_path_broken")
    assert [a.kind for a in AlertStore(engine).for_incident(iid)] == ["M1", "M1u", "M9"]
    assert _worker(engine, lambda model: llm).run_once() is None  # 대기열이 비었다


def test_failed_investigation_is_recorded_and_alert_says_so(engine):
    iid = _opened(engine)

    def broken(model):
        raise RuntimeError("LLM down")

    assert _worker(engine, broken).run_once() == iid

    assert IncidentStore(engine).get(iid).investigation == "failed"
    update = next(a for a in AlertStore(engine).for_incident(iid) if a.kind == "M1u")
    assert "— ⚠️ 조사 실패 " in update.text.splitlines()[0]


def test_superseded_job_leaves_no_result(engine):
    iid = _opened(engine)
    queue = InvestigationQueue(engine)
    job = queue.claim(NOW, timedelta(minutes=10))
    queue.put(iid, 2, job.request, NOW + timedelta(minutes=5))  # 조사 중 지문이 바뀜

    notifier = Notifier(engine, CONFIG, DryRunSender())
    assert not complete_job(engine, notifier, job, None, NOW + timedelta(minutes=6))
    assert IncidentStore(engine).get(iid).investigation == "pending"


def test_worker_watches_for_stalled_checks(engine):
    _opened(engine)
    InvestigationQueue(engine).claim(NOW, timedelta(hours=4))  # 조사할 것은 없게

    _worker(engine, lambda m: None, at=NOW + timedelta(minutes=181)).run_once()

    assert AlertStore(engine).last_sent("M7") is not None


def test_worker_sends_reminders_for_open_incidents(engine):
    iid = _opened(engine)
    InvestigationQueue(engine).claim(NOW, timedelta(hours=4))  # 조사할 것은 없게
    config = CONFIG.model_copy(
        update={
            "slack": SlackConfig(
                alert_target="C0123456789",
                reminders={"critical": ReminderPolicy(first_after="30m", max=1)},
            )
        }
    )
    at = NOW + timedelta(minutes=31)
    notifier = Notifier(engine, config, DryRunSender())

    Worker(engine, config, notifier, llm_factory=lambda m: None, clock=lambda: at).run_once()

    assert AlertStore(engine).count(iid, "M2") == 1


def test_reminder_failure_does_not_stop_worker(engine):
    _opened(engine)

    class Broken(Notifier):
        def check_reminders(self, now):
            raise RuntimeError("boom")

    worker = Worker(
        engine,
        CONFIG,
        Broken(engine, CONFIG, DryRunSender()),
        llm_factory=lambda m: _scripted(),
        make_k8s=lambda: FixtureReader(*raw_cluster("s04/localhost")),
        clock=lambda: NOW + timedelta(seconds=5),
    )

    assert worker.run_once() is not None  # 리마인드가 실패해도 조사는 한다


def test_alert_is_sent_before_worker_can_claim(engine):
    # 1차 알림(M1) 뒤에 대기열에 넣으므로, 작업자가 아무리 빨라도 M1이 먼저다
    iid = _opened(engine)

    _worker(engine, lambda m: _scripted()).run_once()

    kinds = [a.kind for a in AlertStore(engine).for_incident(iid)]
    assert kinds[0] == "M1" and kinds.count("M1") == 1


def test_each_investigation_gets_fresh_clients(engine):
    _opened(engine)
    _opened(engine, at=NOW + timedelta(minutes=1))  # 같은 이슈가 이어짐(대기 추가 없음)
    made = []
    reader = FixtureReader(*raw_cluster("s04/localhost"))
    work = Worker(
        engine,
        CONFIG,
        Notifier(engine, CONFIG, DryRunSender()),
        llm_factory=lambda m: _scripted(),
        make_k8s=lambda: made.append("k8s") or reader,
        make_http=lambda: made.append("http"),
        clock=lambda: NOW + timedelta(minutes=2),
    )

    work.run_once()

    assert made == ["k8s", "http"]


def test_worker_investigation_is_stored_with_its_incident(engine):
    from core.db import InvestigationStore

    iid = _opened(engine)
    llm = _scripted()  # 역할마다 같은 스크립트를 이어 쓴다
    _worker(engine, lambda m: llm).run_once()

    [inv] = InvestigationStore(engine).for_incident(iid)
    assert inv.status == "done" and inv.report is not None and inv.trigger == "schedule"


# ── 리뷰 지적: 끊긴 조사 정리 (#98 / PR #112) ──


def _running_row(engine, started, *, incident_id=None, run_key="kg-worker-1-1", trigger="schedule"):
    from core.db import CheckRunStore, InvestigationStore

    run = CheckRunStore(engine).create("schedule", started)
    store = InvestigationStore(engine)
    return store, store.start(
        check_run_id=run,
        incident_id=incident_id,
        run_key=run_key,
        agent="investigator:v1",
        trigger=trigger,
        now=started,
    )


def test_run_once_marks_stale_running_investigation_interrupted(engine):
    store, iid = _running_row(engine, NOW)

    _worker(engine, lambda m: None, at=NOW + timedelta(minutes=11)).run_once()

    inv = store.get(iid)
    assert (inv.status, inv.error) == ("failed", "INTERRUPTED")


def test_sweep_covers_manual_cli_row_and_resolved_incident(engine):
    iid = _opened(engine)  # 이슈 1, 조사 대기 중
    CheckRunStore(engine).create("schedule", NOW)  # 해결을 기록한 점검 2
    _, manual = _running_row(engine, NOW, run_key="kg-investigate-1", trigger="investigate")
    store, linked = _running_row(engine, NOW, incident_id=iid)
    IncidentStore(engine).resolve(iid, 2, NOW + timedelta(minutes=1))  # 재집힘 전에 해결됨

    _worker(engine, lambda m: None, at=NOW + timedelta(minutes=11)).run_once()

    assert store.get(manual).status == "failed" and store.get(linked).status == "failed"
