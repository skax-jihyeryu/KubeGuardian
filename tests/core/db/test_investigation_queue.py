import threading
from datetime import UTC, datetime, timedelta

import pytest

from core.db import IncidentStore, InvestigationQueue, create_db_engine

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
STALE = timedelta(minutes=10)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _incident(engine, run_id=1):
    return IncidentStore(engine).open("fp", {"features": ["chat"]}, "critical", run_id, NOW)


def test_queued_request_is_claimed_once(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {"gate_path": "A"}, NOW)

    job = queue.claim(NOW, STALE)

    assert (job.incident_id, job.check_run_id, job.request) == (iid, 1, {"gate_path": "A"})
    assert queue.claim(NOW, STALE) is None  # 이미 집힘
    assert IncidentStore(engine).get(iid).investigation == "running"


def test_finished_job_leaves_queue(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {}, NOW)
    job = queue.claim(NOW, STALE)

    assert queue.complete(job, "done", "core_path_broken")
    assert queue.claim(NOW + STALE * 2, STALE) is None


def test_claim_of_dead_worker_expires(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {}, NOW)
    old = queue.claim(NOW, STALE)

    again = queue.claim(NOW + STALE, STALE)

    assert again is not None and again.incident_id == iid and again.token != old.token
    assert not queue.complete(old, "done", None)  # 늦게 끝난 옛 작업자는 결과를 남기지 않는다


def test_requeue_while_running_supersedes_old_claim(engine):
    # 조사 중에 지문이 바뀌면 새 요청이 대기로 들어가고, 옛 조사는 결과를 남기지 않는다
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {"v": 1}, NOW)
    old = queue.claim(NOW, STALE)

    queue.put(iid, 2, {"v": 2}, NOW + timedelta(minutes=5))

    assert not queue.complete(old, "done", None)
    new = queue.claim(NOW + timedelta(minutes=5), STALE)
    assert (new.check_run_id, new.request) == (2, {"v": 2})


def test_resolved_incident_is_not_claimed(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {}, NOW)
    IncidentStore(engine).resolve(iid, 2, NOW)

    assert queue.claim(NOW, STALE) is None


def test_oldest_request_is_claimed_first(engine):
    queue = InvestigationQueue(engine)
    first, second = _incident(engine), _incident(engine)
    queue.put(second, 2, {}, NOW + timedelta(minutes=1))
    queue.put(first, 1, {}, NOW)

    assert queue.claim(NOW + timedelta(minutes=2), STALE).incident_id == first


def test_claim_specific_incident(engine):
    queue = InvestigationQueue(engine)
    a, b = _incident(engine), _incident(engine)
    queue.put(a, 1, {}, NOW)
    queue.put(b, 1, {}, NOW)

    assert queue.claim(NOW, STALE, incident_id=b).incident_id == b
    assert queue.claim(NOW, STALE, incident_id=b) is None


def test_only_one_of_concurrent_workers_claims(tmp_path):
    path = tmp_path / "kg.db"
    engine = create_db_engine(path)
    InvestigationQueue(engine).put(_incident(engine), 1, {}, NOW)
    results, start = [], threading.Barrier(6)

    def contend():
        queue = InvestigationQueue(create_db_engine(path))
        start.wait()
        results.append(queue.claim(NOW, STALE) is not None)

    threads = [threading.Thread(target=contend) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1


def test_complete_records_status_and_severity_atomically(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {}, NOW)
    job = queue.claim(NOW, STALE)

    assert queue.owns(job)
    queue.complete(job, "incomplete", "partial_users")

    incident = IncidentStore(engine).get(iid)
    assert (incident.investigation, incident.final_severity) == ("incomplete", "partial_users")
    assert not queue.owns(job)


def test_resolution_drops_queued_request_and_clears_status(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 1, {}, NOW)
    job = queue.claim(NOW, STALE)

    IncidentStore(engine).resolve(iid, 2, NOW)

    assert IncidentStore(engine).get(iid).investigation is None
    assert not queue.complete(job, "done", None)  # 해결된 이슈의 늦은 결과는 남기지 않는다


def test_claim_can_be_limited_to_one_check(engine):
    queue = InvestigationQueue(engine)
    iid = _incident(engine)
    queue.put(iid, 7, {}, NOW)

    assert queue.claim(NOW, STALE, incident_id=iid, check_run_id=8) is None
    assert queue.claim(NOW, STALE, incident_id=iid, check_run_id=7) is not None
