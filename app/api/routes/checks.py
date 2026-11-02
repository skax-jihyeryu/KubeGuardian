"""점검 상세·단계 조각·근거 서랍 (UI 정의서 §6.3~§6.4). 조회만 한다."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from api.viewmodels.checks import build_check_detail, build_drawer, build_steps
from api.viewmodels.shell import build_shell
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, parse_id, render, templates

router = APIRouter(include_in_schema=False)


@router.get("/p/{p}/checks/{run}", response_class=HTMLResponse)
def check_detail(
    request: Request,
    run: str,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    ev: str | None = None,
):
    run_id = parse_id(run)
    detail = build_check_detail(engine, project, run_id, now)
    if detail is None:
        raise HTTPException(404)
    drawer = None
    if ev is not None:  # 이 점검의 근거만 연다. 다른 점검·없는 번호는 404
        drawer = build_drawer(engine, run_id, parse_id(ev))
        if drawer is None:
            raise HTTPException(404)
    return render(
        request, "checks/detail.html", build_shell(engine, config, now), d=detail, drawer=drawer
    )


@router.get("/p/{p}/checks/{run}/steps", response_class=HTMLResponse)
def check_steps(
    request: Request,
    run: str,
    project: ProjectDep,
    engine: EngineDep,
    now: NowDep,
    live: str | None = None,
):
    """점검 과정 조각. 진행 중일 때만 스스로 다시 부르고, 끝났으면 폴링 속성이 없다.

    폴링 중(live)이던 화면이 끝·중단·실패를 알게 되면 머리와 판정도 바뀌어야 하므로 새로 고친다.
    """
    built = build_steps(engine, parse_id(run), now)
    if built is None:
        raise HTTPException(404)
    steps, keep_polling, _state = built
    response = templates.TemplateResponse(
        request, "checks/_steps.html", {"p": project, "steps": steps, "oob": live is not None}
    )
    if live is not None and not keep_polling:
        response.headers["HX-Refresh"] = "true"
    return response
