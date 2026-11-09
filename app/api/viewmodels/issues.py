"""이슈 목록·보고서·알림 타임라인의 저장된 화면 값."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from api.timefmt import hm
from api.viewmodels.checks import (
    ACTION_LABEL,
    STALE_INVESTIGATION_MINUTES,
    TRIGGER_LABEL,
    Sev,
)
from api.viewmodels.rule_report import RuleReport, build_rule_report
from api.viewmodels.shell import IMPACT, SEVERITY
from core.db import (
    AlertStore,
    CheckResultStore,
    CheckRunStore,
    EvidenceStore,
    IncidentAckStore,
    IncidentStore,
    InvestigationQueue,
    InvestigationStore,
)
from core.db.incidents import Incident
from core.db.investigations import Investigation
from core.db.tables import IncidentCheckRow
from core.guard.redact import redact_text

# 알림 kind(M 번호는 코드·DB 식별자)를 화면에 보이는 이름으로
ALERT_LABEL = {
    "M1": "이슈 알림",
    "M1u": "원인 갱신",
    "M1m": "담당자 호출 답글",
    "M2": "리마인더",
    "M3": "복구 알림",
    "M7": "점검 중단 알림",
    "M9": "원인 확인 답글",
    "M10": "점검 일부 확인 못 함",
    "ACK": "확인",
    "TEST": "연결 확인",
}


_SLACK_ENTITY = re.compile(r"&(amp|lt|gt);")
_SLACK_CHAR = {"amp": "&", "lt": "<", "gt": ">"}


def _unescape_slack(text: str) -> str:
    """Slack용 `&amp;` `&lt;` `&gt;`만 되돌린다. HTML 이스케이프는 템플릿이 한다."""
    return _SLACK_ENTITY.sub(lambda m: _SLACK_CHAR[m.group(1)], text)


@dataclass(frozen=True)
class IssueRow:
    incident: Incident
    title: str
    severity: Sev
    search_text: str = ""


@dataclass(frozen=True)
class Event:
    at: datetime
    label: str
    text: str = ""
    run_id: int | None = None
    dry_run: bool = False
    count: int = 1
    runs: tuple[int, ...] = ()
    report: Investigation | None = None


@dataclass(frozen=True)
class IssueDetail:
    row: IssueRow
    selected: Investigation | None
    timeline: list[Event]
    evidence: dict[str, tuple[int, int]]
    live: bool
    investigation_state: str
    log_evidence: frozenset[str] = frozenset()  # 로그 근거 id. 사전 확인의 로그 사실을 가린다
    ack_state: str = ""  # 머리 줄에 보이는 확인 상태. 긴급 열림이 아니면 비어 있다
    rule_report: RuleReport | None = None  # 규칙 진단 이슈의 규칙 기반 진단 결과 (#185)


def issue_row(engine: Engine, incident: Incident) -> IssueRow:
    investigations = InvestigationStore(engine).for_incident(incident.id)
    # 규칙 진단 이슈는 옛 AI 보고서가 아니라 규칙 기준(AI 없는 이슈와 같은 제목)을 쓴다 (#185)
    latest = (
        None
        if incident.investigation == "rule"
        else next((i.report for i in reversed(investigations) if i.report), None)
    )
    top = latest.top_issue if latest else None
    title = top.title if top else ", ".join(incident.features or incident.services)
    title = title or "장애 신호·기능 점검 실패"
    severity = IMPACT.get(
        incident.final_severity or "",
        SEVERITY.get(incident.first_severity or "", ("risk", "잠재 위험")),
    )
    return IssueRow(
        incident,
        redact_text(title),
        Sev(*severity),
        redact_text(latest.conclusion) if latest else "",
    )


def list_issues(
    engine: Engine,
    status: str = "",
    severity: str = "",
    q: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
):
    rows = [
        issue_row(engine, inc)
        for inc in IncidentStore(engine).list_incidents(since=since, until=until, limit=1000)
    ]
    return [
        r
        for r in rows
        if (not status or r.incident.status == status)
        and (not severity or r.severity.key == severity)
        and (
            not q
            or q.casefold()
            in " ".join(
                (r.title, r.search_text, *r.incident.services, *r.incident.rule_ids)
            ).casefold()
        )
    ]


def build_issue_detail(engine: Engine, incident_id: int, report_id: int | None, now: datetime):
    incident = IncidentStore(engine).get(incident_id)
    if incident is None:
        return None
    investigations = InvestigationStore(engine).for_incident(incident_id)
    selected = (
        next((i for i in investigations if i.id == report_id), None)
        if report_id
        else (investigations[-1] if investigations else None)
    )
    if report_id and selected is None:
        return None
    if report_id is None and incident.investigation == "rule":
        selected = None  # 옛 AI 보고서는 타임라인 항목에서 연다
    queued_run = InvestigationQueue(engine).queued_run(incident_id)
    queued = (
        report_id is None
        and incident.status == "open"
        and incident.investigation in {"pending", "running"}
        and queued_run is not None
        and (selected is None or selected.check_run_id != queued_run)
    )
    rule = report_id is None and incident.investigation == "rule"
    state = "rule" if rule else "queued" if queued else selected.status if selected else "none"
    if state == "running" and (now - selected.started_at).total_seconds() > (
        STALE_INVESTIGATION_MINUTES * 60
    ):
        state = "stalled"
    events = []
    with Session(engine) as session:
        actions = list(
            session.execute(
                select(IncidentCheckRow.check_run_id, IncidentCheckRow.action)
                .where(IncidentCheckRow.incident_id == incident_id)
                .order_by(IncidentCheckRow.check_run_id)
            )
        )
    gate_paths = {}  # 이어짐 점검의 경로 (규칙 점검만 "AI 조사 생략"이라 하지 않는다)
    for run_id, action in actions:
        if action == "continued":
            result = CheckResultStore(engine).get(run_id)
            gate_paths[run_id] = result.gate_path if result else None
    # 규칙 진단은 이 이슈를 마지막으로 본(해결 제외) 경로 A 점검의 적발로 보인다
    rule_report = None
    if rule:
        checks = CheckResultStore(engine)
        for run_id, action in reversed(actions):  # 최근 점검부터 경로 A인 것을 찾는다
            if action != "resolved":
                rule_report = build_rule_report(checks.get(run_id), set(incident.targets))
                if rule_report:
                    break
    for run_id, action in actions:
        run = CheckRunStore(engine).get_run(run_id)
        if run:
            event = Event(
                run[0],
                "점검 · " + ACTION_LABEL.get(action, action),
                TRIGGER_LABEL.get(run[1], run[1]),
                run_id,
            )
            events.append(
                Event(event.at, "이어짐 점검", event.text, run_id)
                if action == "continued"
                else event
            )
    for inv in investigations:
        events.append(
            Event(
                inv.ended_at or inv.started_at,
                "조사 완료"
                if inv.report
                else {"running": "조사 중", "failed": "조사 실패"}.get(inv.status, "조사 미완"),
                f"보고서 R-{inv.id:04d}" if inv.report and inv is selected else "",
                inv.check_run_id,
                report=inv if inv.report and inv is not selected else None,
            )
        )
    for alert in AlertStore(engine).for_incident(incident_id):
        events.append(
            Event(
                alert.sent_at,
                f"Slack · {ALERT_LABEL.get(alert.kind, alert.kind)}",
                redact_text(_unescape_slack(alert.text)),
                dry_run=alert.dry_run,
            )
        )
    timeline = []
    for event in sorted(events, key=lambda e: e.at):
        if event.label == "이어짐 점검" and timeline and timeline[-1].label == event.label:
            prev = timeline.pop()
            timeline.append(
                Event(
                    prev.at,
                    event.label,
                    "같은 문제가 이어짐"
                    if gate_paths.get(event.run_id) == "A"
                    else "같은 문제라 AI 조사 생략",
                    event.run_id,
                    count=prev.count + 1,
                    runs=(*(prev.runs or (prev.run_id,)), event.run_id),
                )
            )
        else:
            timeline.append(event)
    evidence = {}
    log_evidence = set()
    if selected and selected.report:
        for e in EvidenceStore(engine).list_for_run(selected.check_run_id, limit=10000):
            evidence[e.id] = (selected.check_run_id, int(e.id.split("-")[1]))
            if e.kind == "log_excerpt":
                log_evidence.add(e.id)
    ack = IncidentAckStore(engine).get(incident_id)
    ack_state = ""
    if ack:
        ack_state = f"확인 {hm(ack.acked_at)} · {redact_text(ack.user_name)}"
    elif incident.status == "open" and incident.first_severity == "critical":
        ack_state = "미확인"
    return IssueDetail(
        issue_row(engine, incident),
        selected,
        timeline,
        evidence,
        state in {"queued", "running"},
        state,
        frozenset(log_evidence),
        ack_state,
        rule_report,
    )
