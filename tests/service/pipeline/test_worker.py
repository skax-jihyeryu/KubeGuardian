from datetime import timedelta

import pytest

from core.db import AlertStore, IncidentStore, InvestigationQueue, create_db_engine
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
