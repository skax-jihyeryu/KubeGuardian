from datetime import timedelta

import pytest

from core.config import ApiScenario
from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore, create_db_engine
from core.db.investigations import INTERRUPTED
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


def test_missing_configured_service_is_unknown_without_incident(engine):
    from core.db import CheckResultStore

    config = _symptom_config()
    config.api_scenarios[0].steps[0].call.via = "gatewayx"
    http = _http(200)
    out = run_check(_reader(), config, engine, trigger="schedule", now=NOW, http=http)

    assert (out.gate.path, out.gate.severity) == ("C", None)
    assert out.incidents == []
    assert out.gate.symptoms == []
    [symptom] = out.symptoms
    assert (symptom.status, symptom.failed_step, symptom.severity) == ("unknown", None, None)
    assert "Service gatewayx" in symptom.summary
    assert http.calls == []
    [stored] = CheckResultStore(engine).get(out.run_id).symptoms
    assert stored["status"] == "unknown"


def test_missing_configured_service_notifies_only_after_three_checks(engine):
    from core.db import AlertStore
    from service.notify.notifier import Notifier
    from tests.service.notify.test_notifier import FakeSender

    config = _symptom_config()
    config.api_scenarios[0].steps[0].call.via = "gatewayx"
    sender = FakeSender()
    notifier = Notifier(engine, config, sender)
    for index in range(4):
        now = NOW + timedelta(minutes=5 * index)
        out = run_check(_reader(), config, engine, trigger="schedule", now=now, http=_http(200))
        notifier.after_check(out.run_id, out.gate, out.incidents, now + timedelta(seconds=12))
        assert len(sender.posts) == (0 if index < 2 else 1)

    assert [alert.kind for alert in AlertStore(engine).list_alerts()] == ["M10"]


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


# ── 점검 결과·규칙 선택 (#116) ──


def _all_rules_except(*off):
    from service.checks.detectors.catalog import CATALOG

    return set(CATALOG) - set(off), set(CATALOG)


def test_check_result_has_verdicts_gate_and_phase_times(engine):
    from core.db import CheckResultStore

    out = run_check(_reader("h01/detected"), CONFIG, engine, trigger="check", now=NOW)

    got = CheckResultStore(engine).get(out.run_id)
    assert (got.status, got.gate_path, got.first_severity) == ("done", "C", None)
    assert [p.name for p in got.phases] == ["collect", "rules", "symptoms", "gate"]
    assert got.phases[0].ended_at == got.phases[1].started_at
    assert got.phases[0].summary == "수집 성공 9종 · 실패 0종"
    assert got.phases[1].summary == "적발 0 · 관찰 중 0 · 개선 권고 2"
    assert got.phases[0].started_at >= NOW and got.finished_at >= got.phases[-1].ended_at
    by_rule = {v["rule_id"]: v for v in got.verdicts}
    assert by_rule["S01"]["passed"] > 0 and by_rule["S01"]["items"] == []
    assert {i["verdict"] for i in by_rule["H01"]["items"]} == {"detected"}
    assert by_rule["H01"]["items"][0]["evidence_id"].startswith("ev-")


def test_check_result_keeps_symptoms_and_failed_step(engine):
    from core.db import CheckResultStore

    out = run_check(_reader(), _symptom_config(), engine, trigger="check", now=NOW, http=_http(500))

    got = CheckResultStore(engine).get(out.run_id)
    [x] = got.symptoms
    assert (x["scenario"], x["status"], x["failed_step"]) == ("chat", "failed", 1)
    assert x["evidence_id"].startswith("ev-")
    assert (got.gate_path, got.first_severity) == ("B", "critical")
    assert got.phases[2].summary == "chat 1단계 실패"


def test_result_is_running_with_phases_appearing_one_by_one(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.pipeline import check as pipeline

    seen = []
    real_gate = pipeline.decide_gate

    def spy(*args, **kw):
        seen.append(CheckResultStore(engine).get(1))
        return real_gate(*args, **kw)

    monkeypatch.setattr(pipeline, "decide_gate", spy)
    run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    assert seen[0].status == "running"
    assert [p.name for p in seen[0].phases] == ["collect", "rules", "symptoms"]


def test_exception_in_the_flow_marks_the_result_failed(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.pipeline import check as pipeline

    def boom(*a, **kw):
        raise RuntimeError("gate 결함")

    monkeypatch.setattr(pipeline, "decide_gate", boom)
    with pytest.raises(RuntimeError):
        run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    store = CheckResultStore(engine)
    got = store.get(1)
    assert got.status == "failed" and got.finished_at is not None
    assert got.verdicts  # 규칙 단계까지 본 판정은 남는다


def test_a_dead_process_leaves_running_that_is_shown_halted_after_the_lock_ttl(engine, monkeypatch):
    from core.db import CheckResultStore

    monkeypatch.setattr(CheckResultStore, "finish", lambda *a, **kw: None)  # 끝을 못 쓰고 죽음
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    store = CheckResultStore(engine)
    got = store.get(out.run_id)
    assert got.status == "running"
    assert store.display_status(got, NOW + timedelta(seconds=10)) == "running"
    assert store.display_status(got, NOW + timedelta(minutes=10)) == "halted"


def test_result_recording_failure_does_not_stop_the_check(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from core.db import CheckResultStore

    def broken(*a, **kw):
        raise OperationalError("insert", {}, Exception("locked"))

    for name in ("start", "phase", "finish"):
        monkeypatch.setattr(CheckResultStore, name, broken)

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "A"


def test_values_are_masked_before_storing(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.model import Signal
    from service.pipeline import check as pipeline

    def fake(ctx, excluded=frozenset()):
        return [
            Signal(
                rule_id="H01",
                verdict="detected",
                subject="dev/Deployment/agent",
                severity="info",
                values={"db_password": "hunter2"},
            )
        ]

    monkeypatch.setattr(pipeline, "run_detectors", fake)
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    verdicts = CheckResultStore(engine).get(out.run_id).verdicts
    [item] = next(v for v in verdicts if v["rule_id"] == "H01")["items"]
    assert item["values"]["db_password"] == "***"


def test_disabled_rule_does_not_run_and_is_recorded(engine):
    from core.db import CheckResultStore, RuleSelectionStore

    RuleSelectionStore(engine).save(*_all_rules_except("W02", "S01"), NOW)

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)

    assert not {s.rule_id for s in out.signals} & {"W02", "S01"}
    assert out.gate.path == "C"
    got = CheckResultStore(engine).get(out.run_id)
    assert got.excluded_rules == ["S01", "W02"]
    assert {v["rule_id"] for v in got.verdicts}.isdisjoint({"S01", "W02"})


def test_disabling_the_rule_of_an_open_incident_does_not_resolve_it(engine):
    from core.db import IncidentStore, RuleSelectionStore

    first = run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW)
    RuleSelectionStore(engine).save(*_all_rules_except("S01"), NOW)

    later = run_check(_reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(minutes=5))

    assert later.incidents == []
    assert IncidentStore(engine).get(first.incidents[0].incident_id).status == "open"

    RuleSelectionStore(engine).save(*_all_rules_except(), NOW)
    healed = run_check(
        _reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(minutes=10)
    )
    assert [c.action for c in healed.incidents] == ["resolved"]


def test_unknown_rule_is_counted_in_the_rules_summary(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.checks.detectors.base import registered

    def boom(ctx):
        raise ValueError("실패")

    monkeypatch.setitem(registered(), "S01", boom)
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    got = CheckResultStore(engine).get(out.run_id)
    assert got.phases[1].summary.endswith("확인하지 못함 1")
    [v] = [v for v in got.verdicts if v["rule_id"] == "S01"]
    assert "감지기 오류 ValueError" in v["items"][0]["message"]


def test_re_enabled_rule_restarts_its_hold_timer(engine):
    from core.db import RuleSelectionStore

    run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=NOW)
    RuleSelectionStore(engine).save(*_all_rules_except("S02"), NOW)
    run_check(_reader("s02/partial"), CONFIG, engine, trigger="schedule", now=NOW)
    RuleSelectionStore(engine).save(*_all_rules_except(), NOW)

    out = run_check(
        _reader("s02/partial"), CONFIG, engine, trigger="schedule", now=NOW + timedelta(days=1)
    )

    s02 = [s for s in out.signals if s.rule_id == "S02" and s.verdict != "passed"]
    assert [s.verdict for s in s02] == ["observing"]  # 옛 관측으로 detected가 되지 않는다


def test_turning_a_rule_off_clears_w01_hold_timestamp(engine):
    from core.db import RuleSelectionStore

    store = SqliteObservationStore(engine)
    store.touch("W01", "dev/Deployment/agent", NOW, {"updatedReplicas_changed_at": NOW.isoformat()})
    RuleSelectionStore(engine).save(*_all_rules_except("W01"), NOW)

    run_check(_reader(), CONFIG, engine, trigger="schedule", now=NOW + timedelta(days=1))

    assert store.get("W01", "dev/Deployment/agent") is None


def _store_failing(monkeypatch, name, exc):
    from core.db import CheckResultStore

    def broken(*a, **kw):
        raise exc

    monkeypatch.setattr(CheckResultStore, name, broken)


def test_sigterm_during_a_check_closes_the_result_as_failed_and_reraises(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.pipeline import check as pipeline

    def term(*a, **kw):
        raise SystemExit(143)

    monkeypatch.setattr(pipeline, "decide_gate", term)
    with pytest.raises(SystemExit):
        run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    got = CheckResultStore(engine).get(1)
    assert (got.status, got.error) == ("failed", INTERRUPTED)
    assert got.finished_at is not None


def test_recorder_failure_of_any_kind_does_not_fail_the_check(engine, monkeypatch):
    _store_failing(monkeypatch, "finish", ValueError("직렬화 실패"))

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "A"


def test_summary_failure_does_not_fail_the_check(engine, monkeypatch):
    from service.pipeline import check as pipeline

    def broken(signals):
        raise KeyError("Z99")

    monkeypatch.setattr(pipeline, "_rules_summary", broken)

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "A"


def test_recorder_failure_does_not_replace_the_original_exception(engine, monkeypatch):
    from service.pipeline import check as pipeline

    def boom(*a, **kw):
        raise RuntimeError("gate 결함")

    monkeypatch.setattr(pipeline, "decide_gate", boom)
    _store_failing(monkeypatch, "finish", ValueError("기록 결함"))

    with pytest.raises(RuntimeError, match="gate 결함"):
        run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)


def test_failed_check_keeps_the_exception_type_name(engine, monkeypatch):
    from core.db import CheckResultStore
    from service.pipeline import check as pipeline

    def boom(*a, **kw):
        raise RuntimeError("gate 결함")

    monkeypatch.setattr(pipeline, "decide_gate", boom)
    with pytest.raises(RuntimeError):
        run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    assert CheckResultStore(engine).get(1).error == "RuntimeError"


def test_done_check_has_no_error_and_every_ran_rule_has_a_verdict_entry(engine):
    from core.db import CheckResultStore
    from service.checks.detectors.catalog import CATALOG

    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    got = CheckResultStore(engine).get(out.run_id)
    assert got.error is None
    assert [v["rule_id"] for v in got.verdicts] == list(CATALOG)  # 대상이 없는 규칙도 한 줄


def test_disabled_rules_have_no_verdict_entry_but_other_rules_do(engine):
    from core.db import CheckResultStore, RuleSelectionStore
    from service.checks.detectors.catalog import CATALOG

    RuleSelectionStore(engine).save(*_all_rules_except("W02"), NOW)

    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    got = CheckResultStore(engine).get(out.run_id)
    assert [v["rule_id"] for v in got.verdicts] == [r for r in CATALOG if r != "W02"]


def test_collection_is_recorded_before_detectors(engine, monkeypatch):
    from core.db import CheckResultStore, CheckRunStore
    from service.pipeline import check as pipeline

    real = pipeline.run_detectors

    def inspect(ctx, excluded):
        result = CheckResultStore(engine).get(1)
        assert [p.name for p in result.phases] == ["collect"]
        assert CheckRunStore(engine).get_snapshot(1) == ctx.snapshot
        return real(ctx, excluded)

    monkeypatch.setattr(pipeline, "run_detectors", inspect)
    run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)
