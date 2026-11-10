"""저장된 규칙·기능 점검 결과를 상세 화면, 과정 요약, 규칙 기반 보고서용으로 변환한다.

규칙 기반 보고서는 규칙이 확인한 위치와 사실만 담는다 (#185).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.db.check_results import CheckResult
from core.guard.redact import redact_mapping, redact_text
from service.checks.detectors.catalog import CATALOG
from web.viewmodels.common import Sev, evidence_numbers, limited_names
from web.viewmodels.setup import STAGES
from web.viewmodels.shell import SEVERITY


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


MAX_CHARS = 120


def _empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, dict | list | tuple) and not value)


def _text(value: Any, *, in_list: bool = False) -> str:
    """값을 사람이 읽는 한 줄로. 파이썬 repr을 쓰지 않는다.

    사전은 `키=값, …`(중첩은 `키(a=1, b=2)`), 목록은 `; `로 잇는다.
    목록 안 사전은 괄호로 감싸 항목 경계를 보인다. 비어 있는 값은 뺀다. 긴 문자열은 자른다.
    """
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if _empty(v):
                continue
            parts.append(f"{k}({_text(v)})" if isinstance(v, dict) else f"{k}={_text(v)}")
        out = ", ".join(parts)
        return f"({out})" if in_list else out
    if isinstance(value, list | tuple):
        return "; ".join(_text(v, in_list=True) for v in value if not _empty(v))
    text = str(value)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "…"


def _s04_problems(values: dict[str, Any]) -> list[tuple[str, str]]:
    """S04: 비정상 status의 주소만 `ENV=값 → Service '대상' 없음`으로 보인다."""
    out = []
    for a in values.get("addresses") or []:
        if a.get("status") == "declared":
            continue
        env = f"{a.get('env')}={a['value']}" if a.get("value") else str(a.get("env"))
        target = str(a.get("target"))
        if a.get("port") is not None:
            if ":" in target and not target.startswith("["):
                target = f"[{target}]"
            target = f"{target}:{a['port']}"
        out.append(("주소", f"{env} → '{target}' 연결 대상 없음"))
    rest = {k: v for k, v in values.items() if k not in ("service", "addresses")}
    return out + _generic(rest)


def _generic(values: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(k), t) for k, v in values.items() if not _empty(v) and (t := _text(v))]


def format_values(rule_id: str, values: dict[str, Any]) -> list[tuple[str, str]]:
    """규칙 판정 값을 화면용 (키, 문구) 목록으로 만든다."""
    if rule_id == "S04":
        return _s04_problems(values)
    return _generic(values)


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
                    values=format_values(spec.id, redact_mapping(item.get("values") or {})),
                    evidence=evidence_numbers([item.get("evidence_id")]),
                    guidance=spec.guidance,
                )
            )
    if not findings:
        return None
    failed = [
        FailedFeature(
            str(s.get("scenario")),
            redact_text(str(s.get("summary", ""))),
            evidence_numbers([s.get("evidence_id")]),
        )
        for s in result.symptoms
        if s.get("status") == "failed"
    ]
    return RuleReport(result.run_id, result.started_at, findings, failed)


@dataclass(frozen=True)
class Item:
    name: str  # "점검 규칙 W02" / "기능 점검 chat"
    status: str  # 적발 / 관찰 중 / 확인하지 못함 / 실패
    text: str
    css: str  # 상태색 (c-crit ...)
    unknown: bool
    evidence: list[int] = field(default_factory=list)
    values: list[tuple[str, str]] = field(default_factory=list)


RULE_STATUS = {"detected": "적발", "observing": "관찰 중", "unknown": "확인하지 못함"}


def build_check_findings(result: CheckResult) -> tuple[list[Item], str | None, str | None]:
    """점검 상세에 표시할 판정 목록과 통과·권고 문구를 만든다."""

    items: list[Item] = []
    passed = rules_run = advice = 0
    for rule in result.verdicts:
        rid = rule.get("rule_id", "")
        spec = CATALOG.get(rid)
        is_advice = spec is not None and spec.grade == "advice"
        if not is_advice:
            rules_run += 1
            passed += int(rule.get("passed", 0))
        for it in rule.get("items", []):
            verdict = it.get("verdict")
            if is_advice and verdict == "detected":
                advice += 1
                continue
            sev = SEVERITY.get(it.get("severity") or "")
            ev = evidence_numbers([it.get("evidence_id")])
            items.append(
                Item(
                    name=f"점검 규칙 {rid}",
                    status=RULE_STATUS.get(verdict, str(verdict)),
                    text=_item_text(spec.name if spec else "", it),
                    css="c-run" if verdict == "unknown" else f"c-{sev[0]}" if sev else "c-warn",
                    unknown=verdict == "unknown",
                    evidence=ev,
                    values=format_values(
                        rid if verdict == "detected" else "",
                        redact_mapping(it.get("values") or {}),
                    )
                    + (
                        [("입력 정보", "; ".join(f"{k} ({f})" for k, f in spec.inputs))]
                        if spec
                        else []
                    ),
                )
            )
    features_passed: list[str] = []
    for s in result.symptoms:
        status = s.get("status")
        if status == "passed":
            features_passed.append(str(s.get("scenario")))
            continue
        failed = status == "failed"
        items.append(
            Item(
                name=f"기능 점검 {s.get('scenario')}",
                status="실패" if failed else "확인하지 못함",
                text=redact_text(str(s.get("summary", ""))),
                css="c-crit" if failed else "c-run",
                unknown=not failed,
                evidence=evidence_numbers([s.get("evidence_id")]),
            )
        )
    parts = []
    if rules_run:
        parts.append(f"점검 규칙 통과 {passed}건 (장애 신호 점검 규칙 {rules_run}개)")
    if features_passed:
        parts.append(f"기능 점검 통과 {len(features_passed)}건 ({', '.join(features_passed)})")
    return items, " · ".join(parts) or None, f"개선 권고 {advice}건" if advice else None


def _item_text(rule_name: str, it: dict[str, Any]) -> str:
    subject = it.get("subject", "")
    message = it.get("message", "")
    head = f"{rule_name} — {subject}" if rule_name else str(subject)
    return redact_text(f"{head} — {message}" if message else head)


ADVICE_LIMIT = 6


def _advice_action(rid: str, values: dict[str, Any]) -> str:
    """개선 권고의 조치 문구. 판정 조건(values)에 맞춘다. 문구가 없는 규칙은 빈 문자열."""
    if rid == "H01":
        if values.get("probe") is None:
            return "준비 상태 검사가 없습니다"
        return "준비 상태 검사가 포트만 확인합니다"
    if rid == "H02":
        missing = ", ".join(str(m) for m in values.get("missing") or [])
        return "CPU·메모리 요청·제한 일부가 없습니다" + (f": {missing}" if missing else "")
    return ""


@dataclass(frozen=True)
class RuleSummaryFinding:
    status: str  # 적발 / 확인하지 못함 / 관찰 중
    rule_name: str
    rule_id: str
    subject: str
    message: str


@dataclass(frozen=True)
class AdviceLine:
    rule_name: str
    rule_id: str
    action: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class StagePass:
    name: str
    passed: tuple[str, ...]  # 항목이 없고 통과 대상이 있던 규칙 이름
    no_target: tuple[str, ...] = ()  # 확인할 대상이 없던 규칙 이름


@dataclass(frozen=True)
class RuleSummary:
    findings: tuple[RuleSummaryFinding, ...] = ()
    advice: tuple[AdviceLine, ...] = ()
    stages: tuple[StagePass, ...] = ()
    developer_lines: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.findings or self.advice or self.stages or self.developer_lines or self.excluded
        )


def _rule_name(rid: str) -> str:
    spec = CATALOG.get(rid)
    return spec.name if spec else rid


def rule_summary(result: CheckResult) -> RuleSummary:
    """규칙 결과를 적발 → 확인하지 못함·관찰 중 → 단계별 통과 → 개선 권고 순서 값으로 만든다."""
    findings: list[RuleSummaryFinding] = []
    advice: dict[tuple[str, str], list[str]] = {}
    developer: list[str] = []
    state: dict[str, str] = {}  # 규칙 ID → items / pass / no_target
    for rule in result.verdicts:
        rid = rule.get("rule_id", "")
        spec = CATALOG.get(rid)
        if spec:
            developer.append(f"{rid} 입력: " + "; ".join(f"{k} ({f})" for k, f in spec.inputs))
        items = rule.get("items", [])
        state[rid] = (
            "items" if items else "pass" if rule.get("passed", 1) else "no_target"
        )  # passed 필드가 없는 옛 결과는 통과
        for it in items:
            verdict = it.get("verdict")
            if spec and spec.grade == "advice" and verdict == "detected":
                values = it.get("values") or {}
                target = str(it.get("service") or it.get("subject", ""))
                targets = advice.setdefault((rid, _advice_action(rid, values)), [])
                if target not in targets:
                    targets.append(target)
                continue
            findings.append(
                RuleSummaryFinding(
                    RULE_STATUS.get(verdict, str(verdict)),
                    _rule_name(rid),
                    rid,
                    str(it.get("subject", "")),
                    str(it.get("message", "")),
                )
            )
    findings.sort(key=lambda f: 0 if f.status == "적발" else 1)  # 안정 정렬: 나머지는 원래 순서
    stages = []
    for name, ids in STAGES:
        ran = [r for r in ids if r in state]
        passed = tuple(_rule_name(r) for r in ran if state[r] == "pass")
        no_target = tuple(_rule_name(r) for r in ran if state[r] == "no_target")
        if passed or no_target:
            stages.append(StagePass(name, passed, no_target))
    return RuleSummary(
        tuple(findings),
        tuple(
            AdviceLine(_rule_name(rid), rid, action, limited_names(targets, ADVICE_LIMIT))
            for (rid, action), targets in advice.items()
        ),
        tuple(stages),
        tuple(developer),
        tuple(_rule_name(r) for r in result.excluded_rules),
    )
