"""화면 공통 라우트: 헬스 체크, 홈 리다이렉트, 점검 이력 홈 (UI 정의서 §6.3)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.viewmodels.shell import build_shell
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, render
from core.db import CheckRunStore

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
    """점검 이력 홈. 목록·필터는 #29에서 붙인다. 지금은 가장 최근 점검으로 가는 링크만 둔다."""
    latest = CheckRunStore(engine).latest_run()
    return render(
        request,
        "checks/home.html",
        build_shell(engine, config, now),
        latest_run=latest[0] if latest else None,
    )
