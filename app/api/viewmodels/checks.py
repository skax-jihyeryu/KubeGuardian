"""점검 상세 화면 값 (UI 정의서 §6.4). 본 것(판정·점검 과정·근거)만 보이고 결론·조치는 이슈에 둔다.

- 저장된 값은 이미 마스킹돼 있지만 근거 원문·조사 인자 등 다른 출처는 표시 전에 한 번 더 마스킹한다.
- 조사 시작 표기는 gate_path만으로 정하지 않는다. 같은 지문은 다시 조사하지 않으므로
  이슈 연결(incident_check)과 조사 행(investigation)으로 정한다.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import get_close_matches
from typing import Any

from sqlalchemy import Engine

from api.timefmt import hms, md_hm, seconds_text
from api.viewmodels.setup import STAGES
from api.viewmodels.shell import IMPACT, SEVERITY
from core.config import KubeGuardianConfig
from core.db import (
    CheckLockStore,
    CheckResultStore,
    CheckRunStore,
    EvidenceStore,
    IncidentStore,
    InvestigationQueue,
    InvestigationStore,
)
from core.db.check_results import CheckResult, Phase
from core.db.evidence import format_evidence_id
from core.db.evidence_summaries import EvidenceSummary, EvidenceSummaryStore
from core.db.incidents import Incident
from core.db.investigations import Investigation
from core.guard.redact import redact_mapping, redact_text
from service.agent.base import AgentStep
from service.agent.investigator.v1.config import SPEC
from service.checks.detectors._common import container_statuses
from service.checks.detectors.catalog import CATALOG
from service.model import JUDGEMENT_LABEL, ClusterSnapshot

STALE_INVESTIGATION_MINUTES = 10  # worker.STALE_CLAIM과 같다. 이보다 오래 running이면 멈춘 조사
TRIGGER_LABEL = {
    "schedule": "주기 점검",
    "ui": "직접 점검",
    "check": "CLI 점검",
    "investigate": "CLI 조사",
}
ACTION_LABEL = {
    "opened": "열림",
    "continued": "이어짐",
    "changed": "재조사",
    "seen": "확인",
    "resolved": "해결",
}
GATE_REASON = {"A": "장애 신호", "B": "기능 점검 실패"}
JUDGEMENT_DOTS = {"confirmed": 3, "candidate": 2, "guess": 1}
EVIDENCE_LIMIT = 100
EVIDENCE_KIND_LABEL = {
    "detector": "규칙 점검 결과",
    "app_api": "기능 점검·앱 API 호출",
    "resource": "리소스 설정·상태 조회",
    "log_excerpt": "Pod 로그",
    "event": "이벤트",
    "probe_result": "연결 확인",
}
RAW_LIMIT = 50_000  # 서랍에 보이는 원문 길이 상한


@dataclass(frozen=True)
class Sev:
    key: str  # crit / warn / risk / tip
    label: str


@dataclass(frozen=True)
class IssueLink:
    id: int
    label: str  # 열림 / 이어짐 / 재조사 / 확인 / 해결
    status: str  # open / resolved


@dataclass(frozen=True)
class Call:
    tool: str
    args: str
    evidence: list[int]
    running: bool = False


@dataclass(frozen=True)
class StepView:
    title: str
    summary: str = ""
    time: str = ""
    evidence: list[int] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    table: list[tuple[str, str, list[int]]] = field(default_factory=list)  # (이름, 설명, 근거)
    calls: list[Call] = field(default_factory=list)
    state: str = "done"  # done / cur / todo
    progress: tuple[int, int] | None = None  # (도구 호출 수, 문제당 예산)
    collect: list["CollectRow"] = field(default_factory=list)  # 정보 수집 단계
    rules: "RuleSummary | None" = None  # 점검 규칙 단계


@dataclass(frozen=True)
class StepsView:
    run_id: int
    live: bool
    check_steps: list[StepView]
    investigation_title: str | None  # 조사 과정 머리글 (조사가 있을 때)
    investigation_meta: str | None
    investigation_steps: list[StepView]
    investigation_na: str | None  # 조사가 없을 때 이유
    progress: tuple[int, int] | None  # 조사 중이면 (도구 호출 수, 문제당 예산)


@dataclass(frozen=True)
class Item:
    name: str  # "점검 규칙 W02" / "기능 점검 chat"
    status: str  # 적발 / 관찰 중 / 확인하지 못함 / 실패
    text: str
    css: str  # 상태색 (c-crit ...)
    unknown: bool
    evidence: list[int] = field(default_factory=list)
    values: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class GateLine:
    label: str  # 조사 시작 — 기능 점검 실패
    started: bool
    detail: str


@dataclass(frozen=True)
class Header:
    kind: str  # sev / calm / plain
    sev: Sev | None
    stage: str | None
    judgement: tuple[str, int] | None  # (확정, 3)
    issues: list[IssueLink]
    open: bool
    meta: str
    note: str | None  # 상태 한 줄 (점검 중·조사 대기·조사 실패 등)
    conclusion_issue: int | None  # 있으면 "이슈 #n에서 결론 보기" 링크
    unknown: int
    elapsed_seconds: int | None = None

    @property
    def elapsed_text(self) -> str:
        return seconds_text(self.elapsed_seconds or 0)


@dataclass(frozen=True)
class EvidenceRow:
    n: int
    content: str
    subject: str
    kind: str
    stage: str
    time: str


@dataclass(frozen=True)
class CheckDetail:
    run_id: int
    project: str
    title: str
    short_time: str
    state: str  # none / running / halted / failed / done
    header: Header
    items: list[Item]
    passed_text: str | None
    advice_text: str | None
    gate: GateLine | None
    steps: StepsView
    evidence: list[EvidenceRow]
    live: bool
    error: str | None
    corrections: list["Correction"] = field(default_factory=list)
    rule_report: Any = None  # 규칙 기반 진단 결과 (RuleReport, AI 조사가 없을 때만, #185)


@dataclass(frozen=True)
class Correction:
    item: str
    configured: str | None
    candidate: str | None
    reason: str


@dataclass(frozen=True)
class DrawerView:
    n: int
    kind: str
    subject: str
    source: str
    collected: str
    raw: str
    truncated: bool
    summary: EvidenceSummary | None = None
    log_lines: list[str] | None = None  # 로그 근거의 줄. 마스킹은 터미널 박스가 한다
    log_caption: str = ""


# ── 읽기 ────────────────────────────────────────────────────────


@dataclass
class _Loaded:
    run_id: int
    started_at: datetime
    trigger: str
    result: CheckResult | None
    state: str
    links: list[tuple[Incident, str]]
    investigation: Investigation | None
    inv_state: str  # none / running / stalled / done / incomplete / failed
    inv_steps: list[AgentStep]
    queued: bool
    live: bool
    snapshot: ClusterSnapshot | None = None


def _load(engine: Engine, run_id: int, now: datetime) -> _Loaded | None:
    run = CheckRunStore(engine).get_run(run_id)
    if run is None:
        return None
    started_at, trigger = run
    result = CheckResultStore(engine).get(run_id)
    state = "none" if result is None else CheckResultStore.display_status(result, now)
    incidents = IncidentStore(engine)
    links = [
        (inc, action)
        for iid, action in incidents.actions_for_runs([run_id]).get(run_id, [])
        if (inc := incidents.get(iid)) is not None
    ]
    investigations = InvestigationStore(engine)
    invs = investigations.for_check(run_id)
    inv = invs[-1] if invs else None
    inv_state, steps = "none", []
    if inv is not None:
        steps = investigations.steps(inv.id)
        inv_state = inv.status
        if inv.status == "running" and (now - inv.started_at).total_seconds() > (
            STALE_INVESTIGATION_MINUTES * 60
        ):
            inv_state = "stalled"
    queued = False
    rule_path = result is not None and result.gate_path == "A"  # 규칙 감지는 조사를 기다리지 않는다
    if inv is None and not rule_path:
        queue = InvestigationQueue(engine)
        lock = CheckLockStore(engine).current(now) if trigger == "ui" else None
        dispatching = lock is not None and lock.run_id == run_id
        queued = any(
            inc.status == "open"
            and action in ("opened", "changed")
            and (queue.queued_run(inc.id) == run_id or dispatching)
            for inc, action in links
        )
    live = state == "running" or inv_state == "running" or queued
    return _Loaded(
        run_id,
        started_at,
        trigger,
        result,
        state,
        links,
        inv,
        inv_state,
        steps,
        queued,
        live,
        CheckRunStore(engine).get_snapshot(run_id),
    )


def build_check_detail(
    engine: Engine,
    project: str,
    run_id: int,
    now: datetime,
    *,
    config: KubeGuardianConfig | None = None,
) -> CheckDetail | None:
    from api.viewmodels.rule_report import build_rule_report  # rule_report가 이 모듈을 읽는다

    d = _load(engine, run_id, now)
    if d is None:
        return None
    result = d.result
    items, passed_text, advice_text = _judge(result) if result else ([], None, None)
    evidence = _evidence_rows(engine, d)
    return CheckDetail(
        run_id=run_id,
        project=project,
        title=f"점검 #{d.run_id} · {md_hm(d.started_at)}",
        short_time=md_hm(d.started_at),
        state=d.state,
        header=_header(d, items, now),
        items=items,
        passed_text=passed_text,
        advice_text=advice_text,
        gate=_gate(d),
        steps=_steps(d, now),
        evidence=evidence,
        live=d.live,
        error=result.error if result else None,
        corrections=configuration_corrections(engine, run_id, result, config),
        rule_report=build_rule_report(result) if d.investigation is None else None,
    )


def configuration_corrections(
    engine: Engine,
    run_id: int,
    result: CheckResult | None,
    config: KubeGuardianConfig | None,
) -> list[Correction]:
    """확인하지 못한 기능 점검만 저장된 스냅샷과 비교한다. 설정을 자동 수정하지 않는다."""
    if result is None or config is None:
        return []
    unknown = {s.get("scenario") for s in result.symptoms if s.get("status") == "unknown"}
    if not unknown:
        return []
    snapshot = CheckRunStore(engine).get_snapshot(run_id)
    resources = snapshot.items("Service") if snapshot else None
    names = {
        str(r.get("metadata", {}).get("name"))
        for r in resources or []
        if r.get("metadata", {}).get("name")
    }
    corrections = []
    for scenario in config.api_scenarios:
        if scenario.name not in unknown:
            continue
        missing = False
        if resources is not None:
            for number, step in enumerate(scenario.steps, 1):
                service = (step.login or step.call).via
                if service in names:
                    continue
                missing = True
                close = get_close_matches(service, sorted(names), n=1, cutoff=0.6)
                corrections.append(
                    Correction(
                        f"{scenario.name} {number}단계 대상 Service",
                        service,
                        close[0] if close else None,
                        "현재 설정값을 이 점검의 스냅샷에서 찾지 못했습니다. "
                        + (
                            "비슷한 이름을 확인한 뒤 설정 파일에 직접 반영하세요."
                            if close
                            else "비슷한 리소스가 없어 수정값을 제안할 수 없습니다."
                        ),
                    )
                )
        if not missing:
            corrections.append(
                Correction(
                    f"기능 점검 {scenario.name}",
                    None,
                    None,
                    "이 점검의 Service 스냅샷이 없어 설정과 비교할 수 없습니다."
                    if resources is None
                    else "Service 이름만으로 원인을 판단할 수 없습니다. "
                    "위 사유와 계정·포트·API 정의를 확인하세요. "
                    "API 문서와의 자동 비교는 지원하지 않습니다.",
                )
            )
    return corrections


def build_steps(engine: Engine, run_id: int, now: datetime) -> tuple[StepsView, bool, str] | None:
    """단계 조각. (조각 값, 폴링을 계속할지, 점검 상태)."""
    d = _load(engine, run_id, now)
    if d is None:
        return None
    return _steps(d, now), d.live, d.state


def _log_caption(data: dict) -> str:
    """터미널 박스 위 한 줄. 있는 값만 이어 붙인다."""
    span = "~".join(str(data[k]) for k in ("first_time", "last_time") if data.get(k))
    parts = [
        data.get("container"),
        "이전 컨테이너" if data.get("previous") else None,
        f"{data['line_count']}줄" if data.get("line_count") is not None else None,
        span or None,
        "앞부분 더 있음" if data.get("more_before") else None,
    ]
    return redact_text(" · ".join(str(p) for p in parts if p))


def build_drawer(
    engine: Engine, run_id: int, n: int, now: datetime | None = None
) -> DrawerView | None:
    """이 점검에 묶인 근거만 연다. 다른 점검·점검에 묶이지 않은 근거는 없는 것으로 본다."""
    store = EvidenceStore(engine)
    ev_id = format_evidence_id(n)
    if store.check_run_of(ev_id) != run_id:
        return None
    ev = store.get(ev_id)
    if ev is None:
        return None
    is_log = ev.kind == "log_excerpt"
    lines = ev.data.get("lines")
    log_lines = [str(x) for x in lines] if is_log and isinstance(lines, list) else None
    raw = json.dumps(redact_mapping(ev.data), ensure_ascii=False, indent=2, default=str)
    return DrawerView(
        n=n,
        kind=EVIDENCE_KIND_LABEL[ev.kind],
        subject=redact_text(ev.subject),
        source=redact_text(ev.source),
        collected=hms(ev.collected_at),
        raw=raw[:RAW_LIMIT],
        truncated=len(raw) > RAW_LIMIT,
        summary=EvidenceSummaryStore(engine).get(ev_id, now or datetime.now(UTC)),
        log_lines=log_lines,
        log_caption=_log_caption(ev.data) if log_lines is not None else "",
    )


# ── 머리 ────────────────────────────────────────────────────────


def _primary_issue(d: _Loaded) -> tuple[Incident, str] | None:
    """조사한 이슈를 우선하고, 없으면 가장 높은 심각도의 이슈."""
    if not d.links:
        return None
    if d.investigation is not None and d.investigation.incident_id is not None:
        for link in d.links:
            if link[0].id == d.investigation.incident_id:
                return link
    order = {"critical": 0, "warning": 1, "info": 2}
    return min(d.links, key=lambda link: (order.get(link[0].first_severity or "", 3), link[0].id))


def _sev_from(d: _Loaded) -> tuple[Sev | None, str | None]:
    """(심각도, 단계 표기). 조사 보고서가 있으면 그 영향, 없으면 1차 심각도."""
    first = SEVERITY.get(d.result.first_severity or "") if d.result else None
    first_sev = Sev(*first) if first else None
    report = d.investigation.report if d.investigation else None
    top = report.top_issue if report else None
    if top is not None and top.impact in IMPACT:
        final = Sev(*IMPACT[top.impact])
        stage = f"1차 {first_sev.label}" if first_sev and first_sev != final else None
        return final, stage
    if first_sev and (d.queued or d.inv_state == "running"):
        return first_sev, "1차 판정 (조사 전)"
    return first_sev, None


def _judgement(d: _Loaded) -> tuple[str, int] | None:
    report = d.investigation.report if d.investigation else None
    if report is None or report.judgement is None:
        return None
    return JUDGEMENT_LABEL[report.judgement], JUDGEMENT_DOTS[report.judgement]


def _header(d: _Loaded, items: list[Item], now: datetime) -> Header:
    result = d.result
    issues = [
        IssueLink(inc.id, ACTION_LABEL.get(action, action), inc.status) for inc, action in d.links
    ]
    primary = _primary_issue(d)
    unknown = sum(1 for i in items if i.unknown)
    trigger = TRIGGER_LABEL.get(d.trigger, d.trigger)
    when = md_hm(d.started_at)
    if result is None:
        return Header(
            "plain", None, None, None, issues, False, f"{when} · {trigger}", None, None, 0
        )
    if d.state == "running":
        elapsed = max(0, int((now - d.started_at).total_seconds()))
        meta = f"{when} · {trigger} · 점검 중"
        return Header(
            "plain",
            None,
            None,
            None,
            issues,
            False,
            meta,
            "점검이 진행 중입니다",
            None,
            0,
            elapsed_seconds=elapsed,
        )
    if d.state == "halted":
        meta = f"{when} · {trigger} · 점검 중단"
        note = "점검이 끝나지 않고 멈췄습니다. 다음 점검은 정상적으로 시작됩니다."
        return Header("plain", None, None, None, issues, False, meta, note, None, 0)
    duration = (
        seconds_text((result.finished_at - result.started_at).total_seconds())
        if result.finished_at
        else None
    )
    if d.state == "failed":
        meta = f"{when} · {trigger} · 점검 실패" + (f" · {duration}" if duration else "")
        return Header("plain", None, None, None, issues, False, meta, None, None, 0)
    meta = f"{when} · {trigger} · 점검 완료 {duration}" if duration else f"{when} · {trigger}"
    sev, stage = _sev_from(d)
    open_ = any(inc.status == "open" for inc, _ in d.links)
    note, conclusion_issue = None, None
    inv = d.investigation
    if d.inv_state == "running":
        pass  # 진행 표시는 단계 조각(StepsView.progress)이 그린다
    elif d.queued:
        note = "조사 대기 중: 작업자가 곧 시작합니다"
    elif d.inv_state == "stalled":
        note = "조사가 중단됐습니다 (작업자 응답 없음)"
    elif inv is not None:
        if inv.status == "failed":
            note = f"조사 실패 ({redact_text(inv.error or '사유 없음')})"
        elif inv.status == "incomplete":
            note = "조사 미완 (호출·시간 상한에 도달)"
        if primary is not None:
            conclusion_issue = primary[0].id
    kind = "sev" if sev is not None else "calm"
    return Header(
        kind, sev, stage, _judgement(d), issues, open_, meta, note, conclusion_issue, unknown
    )


# ── 판정 ────────────────────────────────────────────────────────

_RULE_STATUS = {"detected": "적발", "observing": "관찰 중", "unknown": "확인하지 못함"}


def _judge(result: CheckResult) -> tuple[list[Item], str | None, str | None]:
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
            ev = _ev_numbers([it.get("evidence_id")])
            items.append(
                Item(
                    name=f"점검 규칙 {rid}",
                    status=_RULE_STATUS.get(verdict, str(verdict)),
                    text=_item_text(spec.name if spec else "", it),
                    css="c-run" if verdict == "unknown" else f"c-{sev[0]}" if sev else "c-warn",
                    unknown=verdict == "unknown",
                    evidence=ev,
                    values=[
                        (str(k), str(v)) for k, v in redact_mapping(it.get("values") or {}).items()
                    ]
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
                evidence=_ev_numbers([s.get("evidence_id")]),
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


def _ev_numbers(ids: list[Any]) -> list[int]:
    out = []
    for ev in ids:
        if isinstance(ev, str) and ev.startswith("ev-") and ev[3:].isdigit():
            out.append(int(ev[3:]))
    return out


# ── 조사 판단 ───────────────────────────────────────────────────


def _gate(d: _Loaded) -> GateLine | None:
    result = d.result
    if result is None or result.gate_path is None:
        return None
    path = result.gate_path
    sev = SEVERITY.get(result.first_severity or "")
    sev_text = f" · 1차 심각도 {sev[1]}" if sev else ""
    why = GATE_REASON.get(path, "사용자 증상")
    if path == "C" and d.investigation is None:
        return GateLine("조사 안 함 — 장애 신호 없음", False, "")
    if d.investigation is not None:
        return GateLine(f"조사 시작 — {why}", True, sev_text.lstrip(" ·"))
    if d.queued:
        return GateLine(
            f"조사 시작 — {why} (대기 중)", True, "작업자가 조사를 가져가면 시작합니다."
        )
    continued = [inc for inc, action in d.links if action == "continued"]
    if continued:
        return GateLine(
            f"조사 안 함 — 이슈 #{continued[0].id}에 이어짐",
            False,
            "같은 문제가 이어져 다시 조사하지 않습니다.",
        )
    if path == "A":
        return GateLine(
            "조사 안 함 — 규칙 기반 진단 결과",
            False,
            "규칙이 장애를 적발해 AI 원인 조사를 하지 않고 규칙 근거로 보여 줍니다.",
        )
    return GateLine("조사 안 함 — 조사 요청이 기록되지 않음", False, "")


# ── 점검 과정 ───────────────────────────────────────────────────

_PHASE_TITLE = {
    "collect": "정보 수집",
    "rules": "점검 규칙 점검",
    "symptoms": "기능 점검",
    "gate": "조사 판단",
}


def _phase_time(p: Phase) -> str:
    return f"{hms(p.started_at)} · {max(0, int((p.ended_at - p.started_at).total_seconds()))}s"


def _check_steps(d: _Loaded) -> list[StepView]:
    result = d.result
    if result is None:
        return []
    by_name = {p.name: p for p in result.phases}
    steps = []
    for name, title in _PHASE_TITLE.items():
        p = by_name.get(name)
        if p is None:
            if d.state == "running":
                steps.append(StepView(title, "아직 하지 않음", state="todo"))
            continue
        steps.append(_phase_view(d, result, p, title))
    return steps


NAME_LIMIT = 8
_PRIMARY_KINDS = ("Pod", "Deployment", "Service", "Node")


@dataclass(frozen=True)
class CollectRow:
    kind: str
    count: int
    status: str
    names: tuple[str, ...]
    failed: str | None
    primary: bool


def _name(obj: dict[str, Any]) -> str:
    return str((obj.get("metadata") or {}).get("name", ""))


def _condition(obj: dict[str, Any], kind: str) -> bool | None:
    """status.conditions의 해당 type이 True면 True, False면 False, 판단할 수 없으면 None."""
    conditions = (obj.get("status") or {}).get("conditions") or []
    for c in conditions:
        if c.get("type") == kind:
            return c.get("status") == "True"
    return None


def _pod_base_name(pod: dict[str, Any]) -> str:
    """ReplicaSet 소유면 소유자 이름에서 pod-template-hash를 뗀 이름. 아니면 Pod 이름 그대로."""
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        name = str(owner.get("name", ""))
        if owner.get("kind") == "ReplicaSet" and "-" in name:
            return name.rsplit("-", 1)[0]
    return _name(pod)


def _limited(names: list[str], limit: int = NAME_LIMIT) -> tuple[str, ...]:
    if len(names) <= limit:
        return tuple(names)
    return (*names[:limit], f"외 {len(names) - limit}개")


def _pod_state(pod: dict[str, Any]) -> str:
    """ready / not_ready / unknown / done / terminating."""
    if (pod.get("status") or {}).get("phase") == "Succeeded":
        return "done"
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "terminating"
    ready = _condition(pod, "Ready")
    if ready is None:
        return "not_ready" if _condition(pod, "PodScheduled") is False else "unknown"
    return "ready" if ready else "not_ready"


def _pod_row(pods: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    counts = {"ready": 0, "not_ready": 0, "unknown": 0, "done": 0, "terminating": 0}
    restarts = 0
    groups: dict[str, int] = {}
    for pod in pods:
        counts[_pod_state(pod)] += 1
        restarts += sum(int(cs.get("restartCount") or 0) for cs in container_statuses(pod))
        base = _pod_base_name(pod)
        groups[base] = groups.get(base, 0) + 1
    parts = [f"Ready {counts['ready']}"]
    for key, label in (
        ("not_ready", "준비 안 됨"),
        ("unknown", "확인 불가"),
        ("done", "완료"),
        ("terminating", "종료 중"),
    ):
        if counts[key]:
            parts.append(f"{label} {counts[key]}")
    parts.append(f"재시작 {restarts}")
    names = [f"{n} ×{c}" if c > 1 else n for n, c in groups.items()]
    return " · ".join(parts), _limited(names)


def _deployment_row(deployments: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    match, unknown = 0, 0
    short = []
    for dep in deployments:
        if "status" not in dep:
            unknown += 1
            continue
        expected = (dep.get("spec") or {}).get("replicas", 1)
        ready = (dep["status"] or {}).get("availableReplicas") or 0
        if ready >= expected:
            match += 1
        else:
            short.append(f"{_name(dep)} {ready}/{expected}")
    text = f"기대 수와 일치 {match}"
    if short:
        text += f" · 부족 {len(short)} ({', '.join(short)})"
    if unknown:
        text += f" · 확인 불가 {unknown}"
    return text, _limited([_name(d) for d in deployments])


def collect_summary(snapshot: ClusterSnapshot | None) -> list[CollectRow]:
    """저장된 스냅샷에서 종류별 건수·상태 요약·이름을 만든다. 내용의 정확성을 검증하지 않는다."""
    if snapshot is None:
        return []
    kinds = set(snapshot.resources) | set(snapshot.failed)
    rows = []
    for kind in sorted(
        kinds,
        key=lambda k: (
            k not in _PRIMARY_KINDS,
            _PRIMARY_KINDS.index(k) if k in _PRIMARY_KINDS else 0,
            k,
        ),
    ):
        if kind in snapshot.failed:
            rows.append(CollectRow(kind, 0, "", (), snapshot.failed[kind], kind in _PRIMARY_KINDS))
            continue
        items = snapshot.resources[kind]
        primary = kind in _PRIMARY_KINDS
        status, names = "", ()
        if kind == "Pod":
            status, names = _pod_row(items)
        elif kind == "Deployment":
            status, names = _deployment_row(items)
        elif kind == "Service":
            no_selector = sum(1 for s in items if not (s.get("spec") or {}).get("selector"))
            status = f"selector 없음 {no_selector}" if no_selector else ""
            names = _limited([_name(s) for s in items])
        elif kind == "Node":
            states = [_condition(n, "Ready") for n in items]
            parts = [f"Ready {states.count(True)}"]
            if states.count(False):
                parts.append(f"준비 안 됨 {states.count(False)}")
            if states.count(None):
                parts.append(f"확인 불가 {states.count(None)}")
            status = " · ".join(parts)
            names = _limited([_name(n) for n in items])
        rows.append(CollectRow(kind, len(items), status, names, None, primary))
    return rows


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
class RuleFinding:
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
    findings: tuple[RuleFinding, ...] = ()
    advice: tuple[AdviceLine, ...] = ()
    stages: tuple[StagePass, ...] = ()
    developer_lines: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


def _rule_name(rid: str) -> str:
    spec = CATALOG.get(rid)
    return spec.name if spec else rid


def rule_summary(result: CheckResult) -> RuleSummary:
    """규칙 결과를 적발 → 확인하지 못함·관찰 중 → 단계별 통과 → 개선 권고 순서 값으로 만든다."""
    findings: list[RuleFinding] = []
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
                RuleFinding(
                    _RULE_STATUS.get(verdict, str(verdict)),
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
            AdviceLine(_rule_name(rid), rid, action, _limited(targets, ADVICE_LIMIT))
            for (rid, action), targets in advice.items()
        ),
        tuple(stages),
        tuple(developer),
        tuple(_rule_name(r) for r in result.excluded_rules),
    )


def _phase_view(d: _Loaded, result: CheckResult, p: Phase, title: str) -> StepView:
    time = _phase_time(p)
    if p.name == "collect":
        snapshot = d.snapshot
        lines = []
        if snapshot is None:
            lines.append("저장된 스냅샷 없음")
        elif snapshot.items("Pod") == []:
            lines.append("주의: Pod가 0개입니다. 대상 네임스페이스와 수집 권한을 확인하세요.")
        lines.append("건수는 저장된 스냅샷 기준이며 내용의 정확성을 검증한 결과가 아닙니다.")
        return StepView(title, p.summary, time, lines=lines, collect=collect_summary(snapshot))
    if p.name == "rules":
        return StepView(title, p.summary, time, rules=rule_summary(result))
    if p.name == "symptoms":
        table = [
            (
                f"{s.get('scenario')}",
                f"{s.get('summary', '')} ({_SYMPTOM_STATUS.get(s.get('status'), '')})",
                _ev_numbers([s.get("evidence_id")]),
            )
            for s in result.symptoms
        ]
        evidence = [n for _, _, ev in table for n in ev]
        return StepView(title, p.summary, time, evidence=evidence, table=table)
    gate = _gate(d)
    body = [x for x in (gate.detail if gate else "", result.gate_reason or "") if x]
    return StepView(title, gate.label if gate else p.summary, time, lines=body)


_SYMPTOM_STATUS = {"passed": "통과", "failed": "실패", "unknown": "확인하지 못함"}


def _steps(d: _Loaded, now: datetime) -> StepsView:
    check_steps = _check_steps(d)
    inv = d.investigation
    if inv is None:
        na = None
        if d.state in ("done",):
            rule = d.result is not None and d.result.gate_path == "A"
            na = (
                "조사 대기 중입니다. 작업자가 조사를 시작하면 이곳에 단계가 이어집니다."
                if d.queued
                else "해당 없음 — 규칙 감지라 AI 조사 없이 규칙 기반 진단 결과로 보여 줍니다."
                if rule
                else "해당 없음 — 조사 판단에서 조사 안 함으로 정해 AI 조사를 시작하지 않았습니다."
            )
        return StepsView(d.run_id, d.live, check_steps, None, None, [], na, None)
    return StepsView(
        d.run_id,
        d.live,
        check_steps,
        f"조사 과정 — {md_hm(d.started_at)} 점검 ({TRIGGER_LABEL.get(d.trigger, d.trigger)})",
        _inv_meta(d, inv, now),
        _inv_steps(d),
        None,
        _progress(d),
    )


def _progress(d: _Loaded) -> tuple[int, int] | None:
    if d.inv_state != "running":
        return None
    return sum(1 for s in d.inv_steps if s.node == "tools"), SPEC.budget.per_issue_tool_calls


def _inv_meta(d: _Loaded, inv: Investigation, now: datetime) -> str:
    calls = sum(1 for s in d.inv_steps if s.node == "tools")
    b = SPEC.budget
    rechecked = any(s.node == "stop_check" and s.detail.get("recheck") for s in d.inv_steps)
    limit_seconds = int(b.seconds + (b.recheck_seconds if rechecked else 0))  # 재조사는 시간 연장
    if inv.usage is not None and inv.ended_at is not None:
        seconds = int((inv.ended_at - inv.started_at).total_seconds())
        tokens = inv.usage.input_tokens + inv.usage.output_tokens
        calls = inv.usage.tool_calls or calls
    else:
        seconds = int((now - inv.started_at).total_seconds())
        tokens = 0
    parts = [
        f"에이전트 {inv.agent}",
        f"모델 {SPEC.models['investigate']}",
        f"도구 {calls}/{b.total_tool_calls}",
        f"{seconds}s/{limit_seconds}s",
    ]
    if tokens:
        parts.append(f"{tokens / 1000:.0f}k tokens")
    return " · ".join(parts)


def _args_text(args: dict[str, Any]) -> str:
    safe = redact_mapping(args)
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in safe.items())


def _inv_steps(d: _Loaded) -> list[StepView]:
    steps = d.inv_steps
    running = d.inv_state == "running"
    out: list[StepView] = []

    pre = next((s for s in steps if s.node == "preliminary"), None)
    if pre is not None:
        ev = _ev_numbers(pre.evidence_ids)
        out.append(
            StepView("사전 확인", f"{len(ev)}건 확인" if ev else "", _step_time(pre), evidence=ev)
        )
    elif running:
        out.append(StepView("사전 확인", "아직 하지 않음", state="todo"))

    triage = next((s for s in steps if s.node == "triage"), None)
    plans = [s for s in steps if s.node == "plan"]
    if triage is not None or plans:
        titles = (triage.detail.get("issues") if triage else None) or []
        summary = " ".join(f"{_CIRCLED[i]} {redact_text(str(t))}" for i, t in enumerate(titles[:9]))
        out.append(StepView("계획", summary, _step_time((plans or [triage])[0])))
    elif running:
        out.append(StepView("계획", "아직 하지 않음", state="todo"))

    tools = [s for s in steps if s.node == "tools"]
    budget = SPEC.budget.per_issue_tool_calls
    if tools or (running and (pre is not None or plans)):
        calls = [
            Call(s.tool or "?", _args_text(s.args), _ev_numbers(s.evidence_ids)) for s in tools
        ]
        evidence = [n for c in calls for n in c.evidence]
        out.append(
            StepView(
                "도구 호출 (가설 검증)",
                f"도구 호출 {len(tools)}회",
                _step_time(tools[0]) if tools else "",
                evidence=evidence,
                calls=calls,
                state="cur" if running else "done",
                progress=(len(tools), budget) if running else None,
            )
        )
    elif running:
        out.append(StepView("도구 호출 (가설 검증)", "아직 하지 않음", state="todo"))

    check = next((s for s in reversed(steps) if s.node == "validate"), None)
    if check is not None:
        out.append(StepView("보고서·검증", _validate_text(check), _step_time(check)))
    elif running:
        out.append(StepView("보고서·검증", "아직 하지 않음", state="todo"))
    return out


_CIRCLED = "①②③④⑤⑥⑦⑧⑨"


def _validate_text(step: AgentStep) -> str:
    if step.detail.get("conclusion_supported") is False:
        return "결론을 뒷받침하는 근거가 부족합니다"
    demoted = step.detail.get("demoted")
    return (
        f"근거 검증 통과 · 추정 {len(demoted) if isinstance(demoted, list) else demoted or 0}문장"
    )


def _step_time(step: AgentStep) -> str:
    seconds = (
        int((step.ended_at - step.started_at).total_seconds()) if step.ended_at is not None else 0
    )
    return f"{hms(step.started_at)} · {seconds}s"


# ── 근거 목록 ───────────────────────────────────────────────────


def _evidence_rows(engine: Engine, d: _Loaded) -> list[EvidenceRow]:
    stage_of: dict[int, str] = {}
    for s in d.inv_steps:
        label = (
            "사전 확인" if s.node == "preliminary" else "가설 검증" if s.node == "tools" else None
        )
        if label:
            for n in _ev_numbers(s.evidence_ids):
                stage_of.setdefault(n, label)
    if d.result:
        for sym in d.result.symptoms:
            for n in _ev_numbers([sym.get("evidence_id")]):
                stage_of.setdefault(n, "기능 점검")
    rows = []
    for ev in EvidenceStore(engine).list_for_run(d.run_id, limit=EVIDENCE_LIMIT):
        assert ev.id is not None
        n = int(ev.id[3:])
        stage = stage_of.get(n) or ("점검 규칙 점검" if ev.kind == "detector" else "—")
        rows.append(
            EvidenceRow(
                n,
                _evidence_summary(ev.kind, ev.data),
                redact_text(ev.subject),
                EVIDENCE_KIND_LABEL[ev.kind],
                stage,
                hms(ev.collected_at),
            )
        )
    return rows


def _evidence_summary(kind: str, data: dict[str, Any]) -> str:
    """저장된 관측값만 짧게 표시한다. 누락된 값으로 성공 여부를 추론하지 않는다."""
    data = redact_mapping(data)
    text = "요약할 결과 필드 없음"
    if data.get("error"):
        text = f"조회 실패: {data['error']}"
    elif kind == "detector":
        parts = [_RULE_STATUS.get(data.get("verdict"), ""), str(data.get("message") or "")]
        text = " · ".join(p for p in parts if p) or text
    elif kind == "app_api":
        if "scenario" in data and isinstance(data.get("steps"), list) and "failed_step" in data:
            count = len(data["steps"])
            failed = data.get("failed_step")
            text = (
                f"{count}단계 · 실패 단계 {failed}" if failed else f"{count}단계 · 실패 단계 없음"
            )
        elif "status" in data:
            text = f"HTTP {data['status']}"
            if data.get("latency_ms") is not None:
                text += f" · {data['latency_ms']}ms"
    elif kind == "resource":
        if "count" in data:
            text = f"{data.get('kind', '리소스')} {data['count']}개"
        elif "services" in data and "edges" in data:
            text = f"서비스 {len(data['services'])}개 · 호출 관계 {len(data['edges'])}개"
    elif kind == "event" and "count" in data:
        text = f"이벤트 {data['count']}건"
        if "warnings" in data:
            text += f" · Warning {data['warnings']}건"
    elif kind == "log_excerpt" and "line_count" in data:
        text = f"로그 {data['line_count']}줄"
    elif kind == "probe_result" and "ok" in data and "fail" in data:
        text = f"성공 {data['ok']}·실패 {data['fail']}"
    return redact_text(text)[:240]
