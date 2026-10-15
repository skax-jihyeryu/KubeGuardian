from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from service.model import (
    ClusterSnapshot,
    Evidence,
    GateResult,
    InvestigationRequest,
    Signal,
    SignalBoard,
    severity_for,
)

NOW = datetime(2026, 10, 20, 1, 12, 3, tzinfo=UTC)


def test_evidence_requires_timezone_aware_time():
    with pytest.raises(ValidationError):
        Evidence(
            kind="resource",
            subject="dev/Pod/agent-x2k",
            collected_at=datetime(2026, 10, 20, 10, 12),
            source="GET /api/v1/namespaces/dev/pods/agent-x2k",
            data={},
        )


def test_snapshot_reports_failed_kind_as_unknown_not_empty():
    snap = ClusterSnapshot(
        namespace="dev",
        collected_at=NOW,
        resources={"Pod": []},
        failed={"EndpointSlice": "403 Forbidden"},
    )

    assert snap.items("Pod") == []
    assert snap.items("EndpointSlice") is None


@pytest.mark.parametrize(
    ("impact", "severity"),
    [
        ("core_path_broken", "critical"),
        ("partial_users", "warning"),
        ("latent_risk", "info"),
        ("improvement", "info"),
    ],
)
def test_severity_follows_impact(impact, severity):
    assert severity_for(impact) == severity


def test_investigation_request_accepts_only_open_gate_paths():
    with pytest.raises(ValidationError):
        InvestigationRequest(trigger="schedule", gate_path="C", signal_board=SignalBoard())


def test_user_symptom_is_limited_to_500_chars():
    with pytest.raises(ValidationError):
        InvestigationRequest(
            trigger="investigate",
            gate_path="B",
            signal_board=SignalBoard(),
            user_symptom="가" * 501,
        )


def test_gate_result_lists_signals_that_opened_it():
    signal = Signal(rule_id="S04", verdict="detected", subject="dev/Deployment/gateway")
    gate = GateResult(path="A", severity="critical", signals=[signal], reason="S04 적발")

    assert gate.signals[0].rule_id == "S04"
