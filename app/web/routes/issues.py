"""이슈 목록과 저장된 조사 보고서."""

from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from web.rendering import ConfigDep, EngineDep, NowDep, ProjectDep, parse_id, render
from web.timefmt import date_range, date_window
from web.viewmodels.checks import build_drawer
from web.viewmodels.issues import build_issue_detail, list_issues
from web.viewmodels.shell import build_shell

router = APIRouter(include_in_schema=False)

PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30}


def dates(date_from: str, date_to: str) -> dict[str, str]:
    start, end = date_range(date_from, date_to)
    return {"date_from": start, "date_to": end}


def window(now, days: int, date_from: str, date_to: str):
    """목록 기간 조건 (since, until). 고른 날짜가 빠른 선택(days)보다 우선한다."""
    start, end = date_range(date_from, date_to)
    if start or end:
        return date_window(start, end)
    return (now - timedelta(days=days) if 0 < days <= 365 else None), None


def filter_query(status, severity, q, days, date_from, date_to) -> str:
    start, end = date_range(date_from, date_to)
    query = {"status": status, "severity": severity, "q": q}
    if not (start or end):  # 날짜가 있으면 빠른 선택은 링크에 싣지 않는다
        query["days"] = days
    return urlencode({**query, **{k: v for k, v in (("from", start), ("to", end)) if v}})


@router.get("/{p}/issues", response_class=HTMLResponse)
def issues_home(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    status: str = "",
    severity: str = "",
    q: str = "",
    days: int = 0,
    period: str = "",
    date_from: Annotated[str, Query(alias="from", max_length=40)] = "",
    date_to: Annotated[str, Query(alias="to", max_length=40)] = "",
):
    days = PERIOD_DAYS.get(period, days)
    return render(
        request,
        "issues/list.html",
        build_shell(engine, config, now),
        rows=list_issues(engine, status, severity, q, *window(now, days, date_from, date_to)),
        days=days,
        status=status,
        severity=severity,
        q=q,
        filter_query=filter_query(status, severity, q, days, date_from, date_to),
        **dates(date_from, date_to),
        d=None,
    )


@router.get("/{p}/issues/{issue}", response_class=HTMLResponse)
def issue_detail(
    request: Request,
    issue: str,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    report: str | None = None,
    ev: str | None = None,
    status: str = "",
    severity: str = "",
    q: str = "",
    days: int = 0,
    period: str = "",
    date_from: Annotated[str, Query(alias="from", max_length=40)] = "",
    date_to: Annotated[str, Query(alias="to", max_length=40)] = "",
):
    detail = build_issue_detail(engine, parse_id(issue), parse_id(report) if report else None, now)
    if detail is None:
        raise HTTPException(404)
    drawer, run_id = None, None
    if ev is not None:
        if detail.rule_report:
            run_id = detail.rule_report.run_id
        elif detail.selected and detail.selected.report:
            run_id = detail.selected.check_run_id
        else:
            raise HTTPException(404)
        drawer = build_drawer(engine, run_id, parse_id(ev), now)
        if drawer is None:
            raise HTTPException(404)
    days = PERIOD_DAYS.get(period, days)
    return render(
        request,
        "issues/detail.html",
        build_shell(engine, config, now),
        rows=list_issues(engine, status, severity, q, *window(now, days, date_from, date_to)),
        status=status,
        severity=severity,
        q=q,
        days=days,
        filter_query=filter_query(status, severity, q, days, date_from, date_to),
        **dates(date_from, date_to),
        d=detail,
        drawer=drawer,
        run_id=run_id,
        drawer_close=request.url.remove_query_params(["ev", "live"]),
        drawer_here=request.url.remove_query_params("live"),
    )
