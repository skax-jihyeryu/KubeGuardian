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


def test_snapshot_kind_not_collected_is_unknown():
    snap = ClusterSnapshot(namespace="dev", collected_at=NOW)

    assert snap.items("Pod") is None


def test_issue_severity_is_serialized():
    from service.model import Issue

    issue = Issue(rank=1, title="채팅 실패", impact="core_path_broken", confidence="high")

    assert issue.model_dump()["severity"] == "critical"


def test_signal_severity_only_when_detected():
    with pytest.raises(ValidationError):
        Signal(rule_id="S02", verdict="observing", subject="dev/Service/agent", severity="critical")


def test_fact_requires_evidence():
    from service.model import Fact

    with pytest.raises(ValidationError):
        Fact(statement="Pod 3개 모두 Ready", evidence_ids=[])


def test_coverage_missing_area_defaults_not_checked():
    from service.model.report import Coverage

    cov = Coverage.model_validate({"run": {"status": "checked", "summary": "x"}})

    assert cov.run.status == "checked"
    assert cov.logs.status == "not_checked"
    assert cov.logs.summary == "보고서에 없음"


def test_coverage_ignores_unknown_keys():
    from service.model.report import Coverage

    cov = Coverage.model_validate({"network": {"status": "checked"}})

    assert not hasattr(cov, "network")


def test_issue_cause_resource_optional():
    from service.model.report import Issue, ResourceRef

    base = {"rank": 1, "title": "t", "impact": "latent_risk", "confidence": "low"}
    assert Issue(**base).cause_resource is None
    issue = Issue(**base, cause_resource={"kind": "Deployment", "name": "agent"})
    assert issue.cause_resource == ResourceRef(kind="Deployment", name="agent")
