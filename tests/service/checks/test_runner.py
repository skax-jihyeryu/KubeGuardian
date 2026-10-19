from datetime import timedelta

import pytest

from service.checks import runner
from service.checks.detectors.base import registered
from service.checks.detectors.catalog import CATALOG
from service.model import Signal
from tests.service.checks.detectors.cases import (
    NOW,
    MemoryObservations,
    context,
    failed_snapshot,
    snapshot,
)


def test_every_catalog_rule_has_a_detector():
    runner.load_detectors()
    assert set(CATALOG) <= set(registered())


def test_missing_detector_is_rejected(monkeypatch):
    monkeypatch.setattr(runner, "registered", lambda: {})
    with pytest.raises(RuntimeError, match="N01"):
        runner.load_detectors()


def test_healthy_cluster_has_no_detected_signal():
    signals = runner.run_detectors(context(snapshot()))
    assert {s.rule_id for s in signals} <= set(CATALOG)
    assert [s for s in signals if s.verdict != "passed"] == []


def test_detector_exception_becomes_unknown_with_masked_message(monkeypatch):
    def boom(ctx):
        raise ValueError("token=abcd1234secret 연결 실패")

    monkeypatch.setitem(registered(), "S01", boom)
    signals = [s for s in runner.run_detectors(context(snapshot())) if s.rule_id == "S01"]
    assert [(s.verdict, s.subject) for s in signals] == [("unknown", "dev")]
    assert "abcd1234secret" not in signals[0].message
    assert "ValueError" in signals[0].message


def test_prune_removes_records_of_subjects_no_longer_seen():
    obs = MemoryObservations()
    obs.seed("S02", "dev/Service/deleted", NOW - timedelta(minutes=30))
    obs.seed("S02", "dev/Service/agent", NOW - timedelta(minutes=30))
    runner.run_detectors(context(snapshot("s02/partial"), observations=obs))
    assert obs.get("S02", "dev/Service/deleted") is None
    assert obs.get("S02", "dev/Service/agent") is not None


def test_prune_is_skipped_when_rule_inputs_are_unknown():
    obs = MemoryObservations()
    obs.seed("S02", "dev/Service/agent", NOW - timedelta(minutes=30))
    signals = runner.run_detectors(context(failed_snapshot("EndpointSlice"), observations=obs))
    assert any(s.rule_id == "S02" and s.verdict == "unknown" for s in signals)
    assert obs.get("S02", "dev/Service/agent") is not None  # 판정 보류


def test_prune_is_skipped_when_detector_raises(monkeypatch):
    obs = MemoryObservations()
    obs.seed("S02", "dev/Service/agent", NOW - timedelta(minutes=30))
    monkeypatch.setitem(registered(), "S02", lambda ctx: 1 / 0)
    runner.run_detectors(context(snapshot(), observations=obs))
    assert obs.get("S02", "dev/Service/agent") is not None


def test_detected_signal_without_severity_is_rejected(monkeypatch):
    monkeypatch.setitem(
        registered(),
        "S01",
        lambda ctx: [Signal(rule_id="S01", verdict="detected", subject="dev/Service/x")],
    )
    s = next(s for s in runner.run_detectors(context(snapshot())) if s.rule_id == "S01")
    assert s.verdict == "unknown"  # 감지기 결함은 확인하지 못함으로 둔다
