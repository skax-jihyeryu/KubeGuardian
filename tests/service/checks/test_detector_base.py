from datetime import UTC, datetime, timedelta

import pytest

from service.checks.detectors.base import detector, evaluate_hold, registered
from service.checks.detectors.catalog import CATALOG

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


def test_detector_for_unknown_rule_is_rejected():
    with pytest.raises(ValueError, match="X99"):
        detector("X99")(lambda ctx: [])


def test_same_rule_cannot_be_registered_twice():
    detector("N01")(lambda ctx: [])

    with pytest.raises(ValueError, match="N01"):
        detector("N01")(lambda ctx: [])

    registered().pop("N01")


@pytest.mark.parametrize(
    ("elapsed", "verdict"),
    [(timedelta(minutes=14), "observing"), (timedelta(minutes=15), "detected")],
)
def test_hold_rule_is_detected_only_after_hold_duration(elapsed, verdict):
    assert evaluate_hold(CATALOG["S02"], first_seen=NOW - elapsed, now=NOW) == verdict


def test_declared_rule_is_detected_immediately():
    assert evaluate_hold(CATALOG["S04"], first_seen=NOW, now=NOW) == "detected"
