"""스냅샷을 읽지 않는 페이지 단위 점검 이력."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import Engine, String, cast, exists, func, or_, select, true
from sqlalchemy.orm import Session, aliased

from core.db import CheckResultStore
from core.db.tables import CheckResultRow, CheckRunRow, IncidentCheckRow, InvestigationRow
from core.guard.redact import redact_text
from core.schedule import stalled_after
from service.checks.detectors.catalog import CATALOG
from service.checks.severity import CHECK_SEVERITIES, check_severity_rank, unknown_count
from web.timefmt import date_range, date_window, md_hm
from web.viewmodels.checks import ACTION_LABEL, TRIGGER_LABEL
from web.viewmodels.common import Sev


@dataclass(frozen=True)
class CheckFilters:
    period: str = "all"
    severity: str = ""
    relation: str = ""
    trigger: str = ""
    status: str = ""
    q: str = ""
    date_from: str = ""  # 주소의 from. KST 날짜 YYYY-MM-DD
    date_to: str = ""  # 주소의 to

    @classmethod
    def from_query(cls, query):
        fields = {
            k: str(query.get(k, "all" if k == "period" else ""))[:200]
            for k in cls.__dataclass_fields__
            if k not in ("date_from", "date_to")
        }
        start, end = date_range(query.get("from"), query.get("to"))
        if start or end:  # 날짜가 빠른 선택보다 우선한다
            fields["period"] = "all"
        return cls(**fields, date_from=start, date_to=end)

    def query(self) -> dict[str, str]:
        """값이 있는 필터만 주소 파라미터 이름으로."""
        dated = bool(self.date_from or self.date_to)
        return {
            {"date_from": "from", "date_to": "to"}.get(k, k): v
            for k, v in vars(self).items()
            if v and not (dated and k == "period")
        }


@dataclass
class CheckRow:
    id: int
    time: str
    severity: Sev | None
    summary: str
    trigger: str
    status: str
    relation: str
    advice: int = 0
    unknown: int = 0
    gap: int = 0
    url: str = ""
    more_issues: int = 0


@dataclass
class CheckPage:
    rows: list[CheckRow]
    filters: CheckFilters
    next_url: str | None
    live: bool = False


STATUS = {"running": "진행 중", "done": "완료", "failed": "중단", "halted": "중단"}


def list_checks(
    engine: Engine,
    filters: CheckFilters,
    cursor: int | None,
    now: datetime,
    project: str = "dev",
    schedule: str = "*/5 * * * *",
) -> CheckPage:
    previous = aliased(CheckRunRow)
    previous_time = (
        select(previous.started_at)
        .where(previous.id < CheckRunRow.id)
        .order_by(previous.id.desc())
        .limit(1)
        .correlate(CheckRunRow)
        .scalar_subquery()
    )
    rank = check_severity_rank()
    stmt = select(
        CheckRunRow.id, CheckRunRow.started_at, CheckRunRow.trigger, previous_time, rank
    ).outerjoin(CheckResultRow, CheckResultRow.run_id == CheckRunRow.id)
    if cursor:
        stmt = stmt.where(CheckRunRow.id < cursor)
    days = {"today": 1, "1d": 1, "7d": 7, "30d": 30}.get(filters.period)
    since, until = date_window(filters.date_from, filters.date_to)
    if since or until:  # 날짜를 고르면 빠른 선택보다 우선한다
        if since:
            stmt = stmt.where(CheckRunRow.started_at >= since)
        if until:
            stmt = stmt.where(CheckRunRow.started_at <= until)
    elif days:
        stmt = stmt.where(CheckRunRow.started_at >= now - timedelta(days=days))
    if filters.severity:
        ranks = {
            "critical": 4,
            "crit": 4,
            "warning": 3,
            "warn": 3,
            "info": 2,
            "risk": 2,
            "tip": 1,
            "calm": 0,
        }
        stmt = stmt.where(rank == ranks.get(filters.severity, -1))
        if filters.severity == "calm":
            stmt = stmt.where(CheckResultRow.status == "done")
    if filters.trigger:
        stmt = stmt.where(CheckRunRow.trigger == filters.trigger)
    if filters.relation:
        relation = exists(
            select(IncidentCheckRow.incident_id).where(
                IncidentCheckRow.check_run_id == CheckRunRow.id
            )
        )
        if filters.relation == "none":
            stmt = stmt.where(~relation)
        else:
            stmt = stmt.where(
                exists(
                    select(IncidentCheckRow.incident_id).where(
                        IncidentCheckRow.check_run_id == CheckRunRow.id,
                        IncidentCheckRow.action == filters.relation,
                    )
                )
            )
    if filters.status:
        from core.db.check_lock import CHECK_LOCK_TTL

        phases = func.json_each(CheckResultRow.phases).table_valued("value")
        heartbeat = (
            select(func.max(func.julianday(func.json_extract(phases.c.value, "$.ended_at"))))
            .select_from(phases)
            .correlate(CheckResultRow)
            .scalar_subquery()
        )
        last_progress = func.max(
            func.julianday(CheckResultRow.started_at),
            func.coalesce(heartbeat, func.julianday(CheckResultRow.started_at)),
        )
        cutoff = func.julianday((now - CHECK_LOCK_TTL).isoformat())
        if filters.status == "halted":
            stmt = stmt.where(
                or_(
                    CheckResultRow.status == "failed",
                    (CheckResultRow.status == "running") & (last_progress < cutoff),
                )
            )
        elif filters.status == "running":
            stmt = stmt.where(CheckResultRow.status == "running", last_progress >= cutoff)
        elif filters.status == "unknown":
            stmt = stmt.where(
                or_(
                    cast(CheckResultRow.verdicts, String).contains('"unknown"'),
                    cast(CheckResultRow.symptoms, String).contains('"unknown"'),
                )
            )
        else:
            stmt = stmt.where(CheckResultRow.status == filters.status)
    if filters.q:

        def json_contains(column):
            text = func.json_tree(column).table_valued("type", "value")
            return exists(
                select(1)
                .select_from(text)
                .where(
                    text.c.type == "text",
                    cast(text.c.value, String).contains(filters.q, autoescape=True),
                )
                .correlate(CheckResultRow)
            )

        titles = func.json_each(InvestigationRow.report, "$.issues").table_valued("value")
        report_title_matches = exists(
            select(1)
            .select_from(InvestigationRow)
            .join(titles, true())
            .where(
                InvestigationRow.check_run_id == CheckRunRow.id,
                func.json_extract(titles.c.value, "$.title").contains(filters.q, autoescape=True),
            )
            .correlate(CheckRunRow)
        )
        stmt = stmt.where(
            or_(
                CheckResultRow.gate_reason.contains(filters.q, autoescape=True),
                json_contains(CheckResultRow.verdicts),
                json_contains(CheckResultRow.symptoms),
                report_title_matches,
            )
        )
    with Session(engine) as session:
        runs = list(session.execute(stmt.order_by(CheckRunRow.id.desc()).limit(51)))
        ids = [r[0] for r in runs[:50]]
        links = (
            list(
                session.execute(
                    select(
                        IncidentCheckRow.check_run_id,
                        IncidentCheckRow.incident_id,
                        IncidentCheckRow.action,
                    ).where(IncidentCheckRow.check_run_id.in_(ids))
                )
            )
            if ids
            else []
        )
    results = CheckResultStore(engine).for_runs(ids)
    rows = []
    query = filters.query()
    if cursor:
        query["cursor"] = cursor
    row_query = "?" + urlencode(query) if query else ""
    for run_id, started, trigger, prior_time, severity_rank in runs[:50]:
        result = results.get(run_id)
        state = CheckResultStore.display_status(result, now) if result else "missing"
        severity = CHECK_SEVERITIES.get(severity_rank)
        relation = " · ".join(f"{ACTION_LABEL.get(a, a)} #{i}" for r, i, a in links if r == run_id)
        advice = (
            sum(
                sum(item.get("verdict") == "detected" for item in v.get("items", []))
                for v in result.verdicts
                if v.get("rule_id") in CATALOG and CATALOG[v["rule_id"]].grade == "advice"
            )
            if result
            else 0
        )
        unknown = unknown_count(result.verdicts, result.symptoms) if result else 0
        if state == "running":
            summary = "점검 진행 중"
        elif state == "halted":
            summary = "점검 중단"
        elif state == "failed":
            summary = "점검 실패"
        else:
            summary = result.gate_reason if result else "이전 점검 — 상세 기록 없음"
        rows.append(
            CheckRow(
                run_id,
                md_hm(started),
                Sev(*severity) if severity else None,
                redact_text(summary or "정상"),
                TRIGGER_LABEL.get(trigger, trigger),
                "일부 확인하지 못함"
                if state == "done" and unknown
                else STATUS.get(state, "기록 없음"),
                relation,
                advice,
                unknown,
                gap=int((started - prior_time).total_seconds() // 60)
                if prior_time and started - prior_time > stalled_after(schedule)
                else 0,
                url=f"/{project}/checks/{run_id}{row_query}",
                more_issues=max(
                    0, sum(r == run_id and action != "resolved" for r, _, action in links) - 1
                ),
            )
        )
    query = filters.query()
    query["cursor"] = rows[-1].id if rows else 0
    next_url = f"/{project}/checks?{urlencode(query)}" if len(runs) > 50 else None
    return CheckPage(rows, filters, next_url, any(r.status == "진행 중" for r in rows))
