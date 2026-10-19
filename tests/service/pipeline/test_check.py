from datetime import timedelta

import pytest

from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore, create_db_engine
from service.pipeline.check import run_check
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster


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
