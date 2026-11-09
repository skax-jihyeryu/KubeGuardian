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
from tests.service.pipeline.test_investigate import SCENARIO_CONFIG
from tests.service.tools.fakes import FakeHttp
from tests.test_cli import _scripted


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _opened(engine, at=NOW):
    """주기 점검이 기능 실패(경로 B) 이슈를 열고 1차 알림을 보낸 상태 (규칙 감지는 AI 조사 없음)."""
    out = run_check(
        FixtureReader(*raw_cluster()),
        SCENARIO_CONFIG,
        engine,
        trigger="schedule",
        now=at,
        http=FakeHttp({("gateway", "/chat"): 500}),
    )
    Notifier(engine, CONFIG, DryRunSender()).after_check(out.run_id, out.gate, out.incidents, at)
    queue_investigations(engine, SCENARIO_CONFIG, out, trigger="schedule", now=at)  # 1차 알림 뒤에
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


def test_worker_logs_the_investigation_flow(engine, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kubeguardian")
    iid = _opened(engine)
    caplog.clear()
    llm = _scripted()
    _worker(engine, lambda model: llm).run_once()

    lines = [r.getMessage() for r in caplog.records if r.name == "kubeguardian"]
    joined = "\n".join(lines)
    assert f"[작업] 조사 대기 집음: 이슈 #{iid}" in lines[0]
    assert any(ln.startswith("[조사 #1] 시작: 이슈 #") for ln in lines)
    assert "[조사 #1] 계획: 가설" in joined
    assert "[조사 #1] 결론:" in joined
    assert "[알림] M1u 발송" in joined and "(dry-run)" in joined
    assert "[알림] M9 발송" in joined
    assert lines[-1].startswith(f"[작업] 이슈 #{iid} 조사 마침: done")
    assert lines.index(next(x for x in lines if "[조사 #1] 결론" in x)) < lines.index(
        next(x for x in lines if "[알림] M1u" in x)
    )


def test_worker_logs_failed_investigation(engine, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kubeguardian")
    iid = _opened(engine)

    def broken(model):
        raise RuntimeError("LLM down")

    _worker(engine, broken).run_once()
    assert any(
        r.getMessage().startswith(f"[작업] 이슈 #{iid} 조사 마침: 실패") for r in caplog.records
    )


def test_failed_result_without_error_does_not_break_worker_log(engine, monkeypatch):
    from service.agent.base import AgentResult

    iid = _opened(engine)
    w = _worker(engine, lambda model: _scripted())
    monkeypatch.setattr(w, "_investigate", lambda job: AgentResult(status="failed", error=None))
    assert w.run_once() == iid


def test_worker_skips_ai_for_rule_request_queued_before_deploy(engine, caplog):
    # 배포 전에 쌓인 경로 A 요청은 AI를 돌리지 않고 규칙 진단으로 마감한다 (#185)
    import logging

    from service.pipeline.investigate import build_request

    out = run_check(
        FixtureReader(*raw_cluster("s04/localhost")), CONFIG, engine, trigger="schedule", now=NOW
    )
    iid = out.incidents[0].incident_id
    request = build_request(out, CONFIG, trigger="schedule", ai_on_rules=True)
    InvestigationQueue(engine).put(iid, out.run_id, request.model_dump(mode="json"), NOW)

    def no_llm(model):
        raise AssertionError("규칙 감지 요청은 LLM을 부르지 않는다")

    with caplog.at_level(logging.INFO):
        assert _worker(engine, no_llm).run_once() == iid

    assert IncidentStore(engine).get(iid).investigation == "rule"
    assert f"[작업] 이슈 #{iid} 규칙 감지 요청이라 AI 조사 생략" in caplog.text
    assert InvestigationQueue(engine).queued_run(iid) is None
    assert [a.kind for a in AlertStore(engine).for_incident(iid)] == []  # 조사 결과 알림 없음
