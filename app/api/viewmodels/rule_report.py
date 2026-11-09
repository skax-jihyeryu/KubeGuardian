"""규칙 기반 진단 결과: AI 조사 없이 규칙이 적발한 점검의 저장된 판정으로 만든 화면 값 (#185).

새 테이블 없이 점검 결과(check_result)의 적발 항목과 실패한 기능 점검을 읽는다.
규칙이 확인한 위치와 사실만 담는다. 근본 원인·영향 범위는 확인하지 않은 것이다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


def _text(value: Any) -> str:
    """값을 사람이 읽는 한 줄로 (목록은 쉼표, 사전은 `키=값`). 파이썬 repr을 쓰지 않는다."""
    if isinstance(value, dict):
        return ", ".join(f"{k}={_text(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _s04_problems(values: dict[str, Any]) -> list[tuple[str, str]]:
    """S04: 비정상 status의 주소만 `ENV=값 → Service '대상' 없음`으로 보인다."""
    out = []
    for a in values.get("addresses") or []:
        if a.get("status") == "declared":
            continue
        env = f"{a.get('env')}={a['value']}" if a.get("value") else str(a.get("env"))
        out.append(("주소", f"{env} → Service '{a.get('target')}' 없음"))
    rest = {k: v for k, v in values.items() if k not in ("service", "addresses")}
    return out + [(k, _text(v)) for k, v in rest.items()]


def _format_values(rule_id: str, values: dict[str, Any]) -> list[tuple[str, str]]:
    if rule_id == "S04":
        return _s04_problems(values)
    return [(str(k), _text(v)) for k, v in values.items()]


def build_rule_report(
    result: CheckResult | None, targets: set[tuple[str, str]] | None = None
) -> RuleReport | None:
    """경로 A 점검의 적발 항목. 장애 신호 적발이 없으면 None.

    targets(이슈의 (규칙 ID, 대상))를 주면 그 이슈의 적발만 남긴다 (한 점검에 이슈 여럿).
    """
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
            if targets and (spec.id, item.get("subject")) not in targets:
                continue
            sev = SEVERITY.get(item.get("severity") or "")
            findings.append(
                RuleFinding(
                    rule_id=spec.id,
                    rule_name=spec.name,
                    location=redact_text(str(item.get("subject", ""))),
                    severity=Sev(*sev) if sev else None,
                    message=redact_text(str(item.get("message", ""))),
                    values=_format_values(spec.id, redact_mapping(item.get("values") or {})),
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
