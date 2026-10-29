from datetime import timedelta

import pytest

from core.config import ApiScenario
from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore, create_db_engine
from service.pipeline.check import run_check
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster
from tests.service.tools.fakes import FakeHttp


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _reader(case=None):
    return FixtureReader(*raw_cluster(case))


def test_healthy_cluster_is_path_c_and_stores_snapshot(engine):
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "C"
    assert CheckRunStore(engine).get_snapshot(out.run_id).namespace == "dev"
    assert out.board.by_service == {}


def test_a1_selector_typo_opens_path_a_and_stores_evidence(engine):
    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "A"
    assert out.gate.severity == "critical"
    s01 = next(s for s in out.gate.signals if s.rule_id == "S01")
    ev = EvidenceStore(engine).get(s01.evidence_id)
    assert ev.kind == "detector"
    assert ev.subject == "dev/Service/agent"
    assert ev.data["values"]["selector"] == {"app": "agnet"}
    assert EvidenceStore(engine).check_run_of(s01.evidence_id) == out.run_id
    assert [s.rule_id for s in out.board.by_service["agent"]] == ["S01"]


def test_a5_localhost_address_opens_path_a(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)
    assert (out.gate.path, [s.rule_id for s in out.gate.signals]) == ("A", ["S04"])


def test_h0_advice_only_is_path_c(engine):
    out = run_check(_reader("h01/detected"), CONFIG, engine, trigger="check", now=NOW)
    assert out.gate.path == "C"
    assert {s.rule_id for s in out.signals if s.verdict == "detected"} == {"H01"}


def test_observations_carry_over_between_checks(engine):
    first = run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=NOW)
    assert first.gate.path == "C"  # 관찰 중
    later = NOW + timedelta(minutes=15)
    second = run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=later)
    assert [s.rule_id for s in second.gate.signals] == ["S02"]
    assert SqliteObservationStore(engine).get("S02", "dev/Service/agent") is not None


def test_previous_snapshot_feeds_restart_increase(engine):
    run_check(_reader("p06/past_restarts"), CONFIG, engine, trigger="schedule", now=NOW)
    out = run_check(
        _reader("p06/restarted_running"),
        CONFIG,
        engine,
        trigger="schedule",
        now=NOW + timedelta(minutes=5),
    )
    p06 = next(s for s in out.signals if s.rule_id == "P06" and s.subject.endswith("x2k9p/agent"))
    assert (p06.verdict, p06.values["previous_restartCount"]) == ("observing", 3)


# ── 증상 감지 (경로 B) ──


def _symptom_config(core_paths=None):
    return CONFIG.model_copy(
        update={
            "core_paths": core_paths or {"chat": ["gateway"]},
            "api_scenarios": [
                ApiScenario.model_validate(
                    {
                        "name": "chat",
                        "feature": "chat",
                        "steps": [
                            {"call": {"via": "gateway", "path": "/chat", "method": "GET"}},
                        ],
                    }
                )
            ],
        }
    )


def _http(status):
    return FakeHttp({("gateway", "/chat"): status})


def test_without_http_symptoms_are_not_checked(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW)
    assert (out.gate.path, out.symptoms) == ("C", [])


def test_rules_pass_and_scenario_fails_opens_path_b(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW, http=_http(500))
    assert (out.gate.path, out.gate.severity) == ("B", "critical")
    [sym] = out.gate.symptoms
    assert (sym.scenario, sym.failed_step) == ("chat", 1)
    ev = EvidenceStore(engine).get(sym.evidence_id)
    assert ev.kind == "app_api"
    assert ev.subject == "dev/Scenario/chat"
    assert EvidenceStore(engine).check_run_of(sym.evidence_id) == out.run_id


def test_passing_scenario_keeps_path_c_but_is_reported(engine):
    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW, http=_http(200))
    assert out.gate.path == "C"
    assert [(s.scenario, s.failed_step) for s in out.symptoms] == [("chat", None)]


def test_signal_keeps_path_a_even_if_scenario_fails(engine):
    out = run_check(
        _reader("s01/detected"),
        _symptom_config(),
        engine,
        trigger="check",
        now=NOW,
        http=_http(500),
    )
    assert (out.gate.path, out.gate.severity) == ("A", "critical")  # 신호 critical, 증상 critical
    assert [s.scenario for s in out.gate.symptoms] == ["chat"]
    assert [s.failed_step for s in out.symptoms] == [1]


def test_user_symptom_opens_path_b_without_http(engine):
    out = run_check(
        _reader(), CONFIG, engine, trigger="investigate", now=NOW, user_symptom="채팅 안 돼요"
    )
    assert (out.gate.path, out.gate.severity) == ("B", "warning")


def test_missing_target_service_opens_path_b_critical(engine):
    objects, failed = raw_cluster()
    objects["Service"] = [x for x in objects["Service"] if x["metadata"]["name"] != "gateway"]
    reader = FixtureReader(objects, failed)
    out = run_check(reader, _symptom_config(), engine, trigger="check", now=NOW, http=_http(200))
    assert (out.gate.path, out.gate.severity) == ("B", "critical")
    assert [(x.scenario, x.status) for x in out.gate.symptoms] == [("chat", "failed")]


def test_blank_user_symptom_is_not_a_symptom(engine):
    out = run_check(_reader(), CONFIG, engine, trigger="investigate", now=NOW, user_symptom="  \n")
    assert out.gate.path == "C"


# ── 점검 잠금 (#88, UI 정의서 §6.5) ──


def _hold(engine, trigger="ui", at=NOW, run_id=None):
    from core.db import CheckLockStore
    from service.pipeline.check import CHECK_LOCK_TTL

    locks = CheckLockStore(engine)
    held = locks.acquire(trigger, at, CHECK_LOCK_TTL)
    if run_id is not None:
        locks.attach_run(held.holder.token, run_id)
    return locks


def _run_ids(engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from core.db.tables import CheckRunRow

    with Session(engine) as s:
        return list(s.scalars(select(CheckRunRow.id)))


@pytest.mark.parametrize("trigger", ["schedule", "check", "ui", "investigate"])
def test_check_does_not_start_while_another_check_holds_the_lock(engine, trigger):
    from core.error.service_exception import ErrorCode, ServiceException

    _hold(engine, run_id=5)

    with pytest.raises(ServiceException) as e:
        run_check(_reader(), CONFIG, engine, trigger=trigger, now=NOW + timedelta(seconds=10))

    assert e.value.error_code is ErrorCode.CHECK_ALREADY_RUNNING
    assert "ui" in e.value.detail and "5" in e.value.detail
    assert _run_ids(engine) == []  # 점검 이력을 만들지 않는다


def test_skipped_schedule_check_is_recorded(engine):
    from core.error.service_exception import ServiceException

    locks = _hold(engine, run_id=5)

    with pytest.raises(ServiceException):
        run_check(_reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(seconds=10))

    [skip] = locks.skips()
    assert (skip.trigger, skip.holder_trigger, skip.holder_run_id) == ("schedule", "ui", 5)


def test_direct_check_refused_by_lock_is_not_recorded_as_skip(engine):
    from core.error.service_exception import ServiceException

    locks = _hold(engine)

    with pytest.raises(ServiceException):
        run_check(_reader(), CONFIG, engine, trigger="check", now=NOW + timedelta(seconds=10))

    assert locks.skips() == []  # 화면·CLI는 409로 알리고 끝난다


def test_lock_holds_run_id_during_check_and_is_released_after(engine):
    from core.db import CheckLockStore

    locks, seen = CheckLockStore(engine), []

    class Peek(FixtureReader):
        def list(self, kind):
            if kind == "Pod":
                seen.append(locks.current(NOW))
            return super().list(kind)

    out = run_check(Peek(*raw_cluster(None)), CONFIG, engine, trigger="schedule", now=NOW)

    assert seen[0].trigger == "schedule" and seen[0].run_id == out.run_id
    assert locks.current(NOW) is None


def test_lock_is_released_when_the_check_fails(engine):
    from core.db import CheckLockStore

    class Broken(FixtureReader):
        def list(self, kind):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_check(Broken(*raw_cluster(None)), CONFIG, engine, trigger="check", now=NOW)

    assert CheckLockStore(engine).current(NOW) is None


def test_expired_lock_of_dead_check_is_taken_over(engine):
    from service.pipeline.check import CHECK_LOCK_TTL

    _hold(engine, trigger="schedule")  # 놓지 못하고 죽은 점검

    out = run_check(_reader(), CONFIG, engine, trigger="schedule", now=NOW + CHECK_LOCK_TTL)

    assert out.run_id in _run_ids(engine)


def test_lock_is_renewed_between_stages_so_long_checks_keep_it(engine, monkeypatch):
    # 화면·CLI 점검은 CronJob처럼 잘리지 않으므로, 단계마다 만료를 늘려 270초를 넘겨도 잠금을 지킨다
    from core.db import CheckLockStore
    from service.pipeline import check as pipeline

    later = NOW + timedelta(seconds=200)
    monkeypatch.setattr(pipeline, "_wall_now", lambda: later)
    locks, seen = CheckLockStore(engine), []

    class Peek(FakeHttp):
        def get(self, **kw):
            seen.append(locks.current(later))
            return super().get(**kw)

    run_check(
        _reader(),
        _symptom_config(),
        engine,
        trigger="ui",
        now=NOW,
        http=Peek({("gateway", "/chat"): 200}),
    )

    assert seen[0].expires_at == later + pipeline.CHECK_LOCK_TTL


def test_failed_skip_record_still_reports_already_running(engine, monkeypatch):
    # 건너뜀 기록이 실패해도 주기 점검은 조용한 건너뜀(CHECK_ALREADY_RUNNING)으로 끝나야 한다
    from sqlalchemy.exc import OperationalError

    from core.db import CheckLockStore
    from core.error.service_exception import ErrorCode, ServiceException

    _hold(engine)

    def broken(*a, **k):
        raise OperationalError("insert", {}, Exception("database is locked"))

    monkeypatch.setattr(CheckLockStore, "record_skip", broken)

    with pytest.raises(ServiceException) as e:
        run_check(_reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(seconds=5))

    assert e.value.error_code is ErrorCode.CHECK_ALREADY_RUNNING


# ── 이슈 (#26) ──


def test_schedule_checks_open_continue_and_resolve_an_incident(engine):
    first = run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW)
    again = run_check(
        _reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW + timedelta(minutes=5)
    )
    healed = run_check(
        _reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(minutes=10)
    )

    assert [(c.action, c.needs_investigation) for c in first.incidents] == [("opened", True)]
    assert [c.action for c in again.incidents] == ["continued"]
    assert [c.action for c in healed.incidents] == ["resolved"]
    assert {c.incident_id for c in [*first.incidents, *again.incidents, *healed.incidents]} == {
        first.incidents[0].incident_id
    }


def test_direct_check_does_not_open_an_incident(engine):
    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "A" and out.incidents == []


def test_schedule_check_queues_investigation_request_for_new_incident(engine):
    from core.db import InvestigationQueue
    from service.model import InvestigationRequest

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW)
    job = InvestigationQueue(engine).claim(NOW, timedelta(minutes=10))

    assert (job.incident_id, job.check_run_id) == (out.incidents[0].incident_id, out.run_id)
    request = InvestigationRequest.model_validate(job.request)
    assert request.trigger == "schedule" and request.gate_path == "A"
    assert "S01" in {s.rule_id for ss in request.signal_board.by_service.values() for s in ss}


def test_continuing_incident_is_not_requeued(engine):
    from core.db import InvestigationQueue

    run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW)
    queue = InvestigationQueue(engine)
    queue.finish(queue.claim(NOW, timedelta(minutes=10)))

    run_check(
        _reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW + timedelta(minutes=5)
    )

    assert queue.claim(NOW + timedelta(minutes=5), timedelta(minutes=10)) is None
