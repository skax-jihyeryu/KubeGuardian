from datetime import UTC, datetime, timedelta

import pytest

from service.checks.detectors.base import detector, evaluate_hold
from service.checks.detectors.catalog import CATALOG

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


def test_detector_for_unknown_rule_is_rejected():
    with pytest.raises(ValueError, match="X99"):
        detector("X99")(lambda ctx: [])


def test_same_rule_cannot_be_registered_twice(monkeypatch):
    # 실제 감지기(n01.py) 등록과 섞이지 않도록 빈 등록부로 바꿔서 확인한다
    monkeypatch.setattr("service.checks.detectors.base._REGISTRY", {})
    detector("N01")(lambda ctx: [])

    with pytest.raises(ValueError, match="N01"):
        detector("N01")(lambda ctx: [])


@pytest.mark.parametrize(
    ("elapsed", "verdict"),
    [(timedelta(minutes=14), "observing"), (timedelta(minutes=15), "detected")],
)
def test_hold_rule_is_detected_only_after_hold_duration(elapsed, verdict):
    assert evaluate_hold(CATALOG["S02"], first_seen=NOW - elapsed, now=NOW) == verdict


def test_declared_rule_is_detected_immediately():
    assert evaluate_hold(CATALOG["S04"], first_seen=NOW, now=NOW) == "detected"


def test_detector_context_carries_previous_snapshot_for_change_rules():
    from core.config import KubeGuardianConfig
    from service.checks.detectors.base import DetectorContext, Observation
    from service.model import ClusterSnapshot

    prev = ClusterSnapshot(namespace="dev", collected_at=NOW - timedelta(minutes=5))
    ctx = DetectorContext(
        snapshot=ClusterSnapshot(namespace="dev", collected_at=NOW),
        previous=prev,
        observations=None,
        config=KubeGuardianConfig(target={"namespace": "dev"}),
        now=NOW,
    )

    assert ctx.previous is prev
    # 관측 기록은 판정에 쓴 값(예: W01의 updatedReplicas)을 함께 남긴다
    obs = Observation(first_seen=NOW, last_seen=NOW, values={"updatedReplicas": 2})
    assert obs.values["updatedReplicas"] == 2
