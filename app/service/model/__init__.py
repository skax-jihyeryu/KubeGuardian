"""도메인 모델.

ClusterSnapshot, Evidence, Signal, SignalBoard, GateResult,
InvestigationRequest, Issue, Hypothesis, Report.
"""

from service.model.evidence import Evidence, EvidenceKind
from service.model.gate import GatePath, GateResult, Symptom
from service.model.investigation import InvestigationRequest, Trigger
from service.model.report import (
    CheckedScope,
    Confidence,
    Fact,
    Hypothesis,
    HypothesisStatus,
    Impact,
    Issue,
    Report,
    severity_for,
)
from service.model.signal import Severity, Signal, SignalBoard, Verdict
from service.model.snapshot import ClusterSnapshot
from service.model.topology import EdgeStatus, Topology, TopologyEdge

__all__ = [
    "CheckedScope",
    "ClusterSnapshot",
    "Confidence",
    "EdgeStatus",
    "Evidence",
    "EvidenceKind",
    "Fact",
    "GatePath",
    "GateResult",
    "Hypothesis",
    "HypothesisStatus",
    "Impact",
    "InvestigationRequest",
    "Issue",
    "Report",
    "Severity",
    "Signal",
    "SignalBoard",
    "Symptom",
    "Topology",
    "TopologyEdge",
    "Trigger",
    "Verdict",
    "severity_for",
]
