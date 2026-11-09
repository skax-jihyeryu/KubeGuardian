"""규칙 기반 진단 결과: AI 조사 없이 규칙이 적발한 점검의 저장된 판정으로 만든 화면 값 (#185).

새 테이블 없이 점검 결과(check_result)의 적발 항목과 실패한 기능 점검을 읽는다.
규칙이 확인한 위치와 사실만 담는다. 근본 원인·영향 범위는 확인하지 않은 것이다.
"""

from dataclasses import dataclass
from datetime import datetime

from api.viewmodels.checks import Sev, _ev_numbers
from api.viewmodels.shell import SEVERITY
from core.db.check_results import CheckResult
from core.guard.redact import redact_mapping, redact_text
from service.checks.detectors.catalog import CATALOG


@dataclass(frozen=True)
class RuleFinding:
    rule_id: str
    rule_name: str
    location: str  # namespace/Kind/이름
    severity: Sev | None
    message: str
    values: list[tuple[str, str]]
    evidence: list[int]
    guidance: str


@dataclass(frozen=True)
class FailedFeature:
    scenario: str
    summary: str
    evidence: list[int]


@dataclass(frozen=True)
class RuleReport:
    run_id: int
    observed_at: datetime  # 규칙을 점검한 시각
    findings: list[RuleFinding]  # 적발마다 하나. 규칙을 하나의 원인으로 묶지 않는다
    failed_features: list[FailedFeature]


def build_rule_report(result: CheckResult | None) -> RuleReport | None:
    """경로 A 점검의 적발 항목. 장애 신호 적발이 없으면 None."""
    if result is None or result.gate_path != "A":
        return None
    findings = []
    for rule in result.verdicts:
        spec = CATALOG.get(rule.get("rule_id", ""))
        if spec is None or spec.grade != "signal":
            continue
        for item in rule.get("items", []):
            if item.get("verdict") != "detected":
                continue
            sev = SEVERITY.get(item.get("severity") or "")
            findings.append(
                RuleFinding(
                    rule_id=spec.id,
                    rule_name=spec.name,
                    location=redact_text(str(item.get("subject", ""))),
                    severity=Sev(*sev) if sev else None,
                    message=redact_text(str(item.get("message", ""))),
                    values=[
                        (str(k), str(v))
                        for k, v in redact_mapping(item.get("values") or {}).items()
                    ],
                    evidence=_ev_numbers([item.get("evidence_id")]),
                    guidance=spec.guidance,
                )
            )
    if not findings:
        return None
    failed = [
        FailedFeature(
            str(s.get("scenario")),
            redact_text(str(s.get("summary", ""))),
            _ev_numbers([s.get("evidence_id")]),
        )
        for s in result.symptoms
        if s.get("status") == "failed"
    ]
    return RuleReport(result.run_id, result.started_at, findings, failed)
