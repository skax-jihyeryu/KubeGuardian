"""이슈 목록과 저장된 조사 보고서."""

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from api.viewmodels.issues import build_issue_detail, list_issues
from api.viewmodels.shell import build_shell
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, parse_id, render

router = APIRouter(include_in_schema=False)


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
):
    return render(
        request,
        "issues/list.html",
        build_shell(engine, config, now),
        rows=list_issues(
            engine, status, severity, q, now - timedelta(days=days) if 0 < days <= 365 else None
        ),
        days=days,
        status=status,
        severity=severity,
        q=q,
        filter_query=urlencode({"status": status, "severity": severity, "q": q, "days": days}),
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
    status: str = "",
    severity: str = "",
    q: str = "",
    days: int = 0,
):
    detail = build_issue_detail(engine, parse_id(issue), parse_id(report) if report else None, now)
    if detail is None:
        raise HTTPException(404)
    return render(
        request,
        "issues/detail.html",
        build_shell(engine, config, now),
        rows=list_issues(
            engine, status, severity, q, now - timedelta(days=days) if 0 < days <= 365 else None
        ),
        status=status,
        severity=severity,
        q=q,
        days=days,
        filter_query=urlencode({"status": status, "severity": severity, "q": q, "days": days}),
        d=detail,
    )
