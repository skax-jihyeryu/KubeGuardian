from datetime import timedelta

import pytest

from core.db import AlertStore, IncidentStore, InvestigationQueue, create_db_engine
from service.notify.notifier import Notifier
from service.notify.sender import DryRunSender
from service.pipeline.check import run_check
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
    return out.incidents[0].incident_id


def _worker(engine, llm_factory, at=NOW + timedelta(seconds=5)):
    reader = FixtureReader(*raw_cluster("s04/localhost"))
    notifier = Notifier(engine, CONFIG, DryRunSender())
    return Worker(engine, CONFIG, notifier, llm_factory=llm_factory, k8s=reader, clock=lambda: at)


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
    assert update.text.startswith("⚠️ 조사 실패")


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
    InvestigationQueue(engine).claim(NOW, timedelta(minutes=10))  # 조사할 것은 없게

    _worker(engine, lambda m: None, at=NOW + timedelta(minutes=16)).run_once()

    assert AlertStore(engine).last_sent("M7") is not None
