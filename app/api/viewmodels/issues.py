"""이슈 목록·보고서·알림 타임라인의 저장된 화면 값."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from api.viewmodels.checks import (
    ACTION_LABEL,
    STALE_INVESTIGATION_MINUTES,
    TRIGGER_LABEL,
    Sev,
)
from api.viewmodels.shell import IMPACT, SEVERITY
from core.db import (
    AlertStore,
    CheckRunStore,
    EvidenceStore,
    IncidentStore,
    InvestigationQueue,
    InvestigationStore,
)
from core.db.incidents import Incident
from core.db.investigations import Investigation
from core.db.tables import IncidentCheckRow
from core.guard.redact import redact_text


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


def issue_row(engine: Engine, incident: Incident) -> IssueRow:
    investigations = InvestigationStore(engine).for_incident(incident.id)
    latest = next((i.report for i in reversed(investigations) if i.report), None)
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
):
    rows = [
        issue_row(engine, inc)
        for inc in IncidentStore(engine).list_incidents(since=since, limit=1000)
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
    queued_run = InvestigationQueue(engine).queued_run(incident_id)
    queued = (
        report_id is None
        and incident.status == "open"
        and incident.investigation in {"pending", "running"}
        and queued_run is not None
        and (selected is None or selected.check_run_id != queued_run)
    )
    state = "queued" if queued else selected.status if selected else "none"
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
                alert.sent_at, "Slack " + alert.kind, redact_text(alert.text), dry_run=alert.dry_run
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
                    "같은 문제라 AI 조사 생략",
                    event.run_id,
                    count=prev.count + 1,
                    runs=(*(prev.runs or (prev.run_id,)), event.run_id),
                )
            )
        else:
            timeline.append(event)
    evidence = {}
    if selected and selected.report:
        for e in EvidenceStore(engine).list_for_run(selected.check_run_id, limit=10000):
            evidence[e.id] = (selected.check_run_id, int(e.id.split("-")[1]))
    return IssueDetail(
        issue_row(engine, incident),
        selected,
        timeline,
        evidence,
        state in {"queued", "running"},
        state,
    )
