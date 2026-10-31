"""도메인 모델.

ClusterSnapshot, Evidence, Signal, SignalBoard, GateResult,
InvestigationRequest, Issue, Hypothesis, Report.
"""

from service.model.chat import AnswerSentence, ChatAnswer, ChatRequest, ChatTurn, SentenceKind
from service.model.evidence import Evidence, EvidenceKind
from service.model.gate import GatePath, GateResult, Symptom
from service.model.investigation import InvestigationRequest, Trigger
from service.model.report import (
    AREAS,
    Area,
    AreaCoverage,
    Confidence,
    Coverage,
    CoverageStatus,
    Fact,
    Hypothesis,
    HypothesisStatus,
    Impact,
    Issue,
    Report,
    ResourceRef,
    severity_for,
)
from service.model.signal import Severity, Signal, SignalBoard, Verdict
from service.model.snapshot import ClusterSnapshot
from service.model.topology import EdgeStatus, Topology, TopologyEdge

__all__ = [
    "AREAS",
    "Area",
    "AreaCoverage",
    "AnswerSentence",
    "ChatAnswer",
    "ChatRequest",
    "ChatTurn",
    "ClusterSnapshot",
    "Confidence",
    "Coverage",
    "CoverageStatus",
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
    "ResourceRef",
    "SentenceKind",
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
