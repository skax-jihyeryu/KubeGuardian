from datetime import timedelta

import pytest

from core.db import IncidentStore, create_db_engine
from service.alerts.incidents import track
from service.checks.gate import decide_gate
from service.model import Signal, Symptom
from tests.service.alerts.test_fingerprint import CONFIG
from tests.service.checks.detectors.cases import NOW


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _sig(rule="S01", service="agent", verdict="detected", subject=None):
    return Signal(
        rule_id=rule,
        verdict=verdict,
        subject=subject or f"dev/Service/{service}",
        service=service,
        severity="critical" if verdict == "detected" else None,
    )


def _sym(status="failed", scenario="chat", step=2):
    return Symptom(
        scenario=scenario,
        status=status,
        failed_step=step if status == "failed" else None,
        summary="",
        severity="critical" if status == "failed" else None,
    )


class Checks:
    """점검을 차례로 흉내 낸다. run_id는 1부터, 시각은 5분씩."""

    def __init__(self, engine):
        self.engine, self.n = engine, 0

    def run(self, signals=(), symptoms=(), trigger="schedule", user=None):
        self.n += 1
        return track(
            self.engine,
            CONFIG,
            run_id=self.n,
            trigger=trigger,
            now=NOW + timedelta(minutes=5 * self.n),
            gate=decide_gate(list(signals), list(symptoms), user),
            signals=list(signals),
            symptoms=list(symptoms),
        )


def _actions(changes):
    return [c.action for c in changes]


def test_schedule_check_with_signal_opens_incident_needing_investigation(engine):
    [c] = Checks(engine).run([_sig()])

    assert (c.action, c.needs_investigation, c.severity) == ("opened", True, "critical")
    inc = IncidentStore(engine).get(c.incident_id)
    assert (inc.status, inc.features, inc.investigation) == ("open", ("chat",), "pending")


def test_path_c_opens_nothing(engine):
    assert Checks(engine).run([_sig(verdict="passed")]) == []


def test_same_fingerprint_continues_without_investigation(engine):
    checks = Checks(engine)
    [first] = checks.run([_sig()])

    [again] = checks.run([_sig()])

    assert (again.action, again.incident_id, again.needs_investigation) == (
        "continued",
        first.incident_id,
        False,
    )
    assert len(IncidentStore(engine).open_incidents()) == 1


def test_changed_fingerprint_on_same_feature_reinvestigates_same_incident(engine):
    checks = Checks(engine)
    [first] = checks.run([_sig("S01", "agent")])

    [changed] = checks.run([_sig("S01", "agent"), _sig("P03", "agent", subject="dev/Pod/a/agent")])

    assert (changed.action, changed.incident_id, changed.needs_investigation) == (
        "changed",
        first.incident_id,
        True,
    )
    assert IncidentStore(engine).get(first.incident_id).rule_ids == ("P03", "S01")


def test_overlapping_feature_keeps_one_incident(engine):
    checks = Checks(engine)
    [chat] = checks.run([_sig("S01", "agent")])

    changes = checks.run([_sig("S01", "agent"), _sig("S01", "admin")])  # login도 영향

    # 기능이 겹치면(chat) 같은 이슈로 잇는다 (점검 하나 = 이슈 하나)
    assert [(c.action, c.incident_id) for c in changes] == [("changed", chat.incident_id)]
    assert IncidentStore(engine).get(chat.incident_id).features == ("chat", "login")


def test_feature_with_no_overlap_opens_new_incident(engine):
    checks = Checks(engine)
    [chat] = checks.run([_sig("S01", "agent")])

    # agent는 이번에 확인하지 못했고(해결 아님), admin(login)만 새로 깨졌다
    [login] = checks.run([_sig("S01", "agent", verdict="unknown"), _sig("S04", "admin")])

    assert login.action == "opened" and login.incident_id != chat.incident_id
    assert len(IncidentStore(engine).open_incidents()) == 2


def test_incident_resolves_when_a_full_check_no_longer_sees_it(engine):
    checks = Checks(engine)
    [opened] = checks.run([_sig()], [_sym()])

    [resolved] = checks.run([_sig(verdict="passed")], [_sym("passed")])

    assert (resolved.action, resolved.incident_id) == ("resolved", opened.incident_id)
    inc = IncidentStore(engine).get(opened.incident_id)
    assert inc.status == "resolved" and inc.resolved_at == NOW + timedelta(minutes=10)


@pytest.mark.parametrize(
    ("signals", "symptoms"),
    [
        ([_sig(verdict="unknown")], [_sym("passed")]),  # 그 대상을 확인하지 못함
        ([_sig(verdict="observing")], [_sym("passed")]),  # 아직 관찰 중
        ([_sig(verdict="unknown", subject="dev")], [_sym("passed")]),  # 규칙 전체 확인 못 함
        ([_sig(verdict="passed")], [_sym("unknown")]),  # 시나리오를 실행하지 못함
        ([_sig(verdict="passed")], []),  # 증상 감지를 하지 않은 점검
    ],
)
def test_incomplete_check_does_not_resolve(engine, signals, symptoms):
    checks = Checks(engine)
    checks.run([_sig()], [_sym()])

    assert checks.run(signals, symptoms) == []
    assert len(IncidentStore(engine).open_incidents()) == 1


def test_direct_check_resolves_but_never_opens(engine):
    checks = Checks(engine)
    assert checks.run([_sig()], trigger="check") == []  # 직접 점검은 이슈를 열지 않는다

    [opened] = checks.run([_sig()])
    [seen] = checks.run([_sig()], trigger="ui")
    [resolved] = checks.run([_sig(verdict="passed")], trigger="check")

    assert (seen.action, seen.needs_investigation) == ("seen", False)
    assert (resolved.action, resolved.incident_id) == ("resolved", opened.incident_id)


def test_new_incident_elsewhere_and_old_resolution_in_one_check(engine):
    checks = Checks(engine)
    [old] = checks.run([_sig("S01", "agent")])

    changes = checks.run([_sig("S01", "agent", verdict="passed"), _sig("P06", "redis")])

    assert sorted(_actions(changes)) == ["opened", "resolved"]
    assert {c.incident_id for c in changes if c.action == "resolved"} == {old.incident_id}


def test_check_links_are_many_to_many(engine):
    checks = Checks(engine)
    [opened] = checks.run([_sig()])
    checks.run([_sig()])
    checks.run([_sig(verdict="passed")])

    assert IncidentStore(engine).check_ids(opened.incident_id) == [1, 2, 3]


def test_final_severity_is_stored_from_report_impact(engine):
    [c] = Checks(engine).run([_sig()])
    store = IncidentStore(engine)

    store.set_final_severity(c.incident_id, "partial_users")

    assert store.get(c.incident_id).final_severity == "partial_users"


def test_pending_investigations_lists_opened_and_changed(engine):
    checks = Checks(engine)
    [c] = checks.run([_sig()])
    store = IncidentStore(engine)

    assert [i.id for i in store.pending_investigations()] == [c.incident_id]
    store.set_investigation(c.incident_id, "done")
    checks.run([_sig()])  # 같은 지문: 다시 대기하지 않는다
    assert store.pending_investigations() == []
