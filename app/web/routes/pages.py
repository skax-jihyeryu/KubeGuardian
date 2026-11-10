"""화면 공통 라우트: 헬스 체크, 홈 리다이렉트, 점검 이력 홈 (UI 정의서 §6.3)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.rendering import ConfigDep, EngineDep, NowDep, ProjectDep, render, templates
from web.viewmodels.check_list import CheckFilters, list_checks
from web.viewmodels.shell import build_shell

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/")
def home(config: ConfigDep) -> RedirectResponse:
    return RedirectResponse(f"/{config.project.id}/checks")


@router.get("/{p}/checks", response_class=HTMLResponse)
def checks_home(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
):
    """필터·페이지 단위 점검 이력."""
    cursor = request.query_params.get("cursor")
    from web.rendering import parse_id

    page = list_checks(
        engine,
        CheckFilters.from_query(request.query_params),
        parse_id(cursor) if cursor else None,
        now,
        project,
        schedule=config.schedule,
    )
    return render(request, "checks/home.html", build_shell(engine, config, now), page=page)


@router.get("/{p}/shell", response_class=HTMLResponse)
def shell_header(
    request: Request, project: ProjectDep, engine: EngineDep, config: ConfigDep, now: NowDep
):
    return templates.TemplateResponse(
        request,
        "_gnb.html",
        {
            "shell": build_shell(engine, config, now),
            "p": project,
        },
    )
