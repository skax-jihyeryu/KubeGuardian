"""점검 상세·단계 조각·근거 서랍 (UI 정의서 §6.3~§6.4). 요약은 별도로 생성·저장한다."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.deps import get_evidence_summary_factory
from api.viewmodels.check_list import CheckFilters, list_checks
from api.viewmodels.checks import build_check_detail, build_drawer, build_steps
from api.viewmodels.shell import build_shell
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, parse_id, render, templates
from core.config import get_setting
from core.db.evidence import format_evidence_id
from service.agent.base import LlmFactory
from service.evidence_summary import summarize_evidence
from service.pipeline.manual_check import ManualCheckConflict, start_manual_check

router = APIRouter(include_in_schema=False)


@router.get("/{p}/checks/{run}", response_class=HTMLResponse)
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
    detail = build_check_detail(engine, project, run_id, now, config=config)
    if detail is None:
        raise HTTPException(404)
    drawer = None
    if ev is not None:  # 이 점검의 근거만 연다. 다른 점검·없는 번호는 404
        drawer = build_drawer(engine, run_id, parse_id(ev), now)
        if drawer is None:
            raise HTTPException(404)
    return render(
        request,
        "checks/detail.html",
        build_shell(engine, config, now),
        d=detail,
        drawer=drawer,
        page=list_checks(
            engine,
            CheckFilters.from_query(request.query_params),
            parse_id(request.query_params["cursor"])
            if request.query_params.get("cursor")
            else None,
            now,
            project,
            schedule=config.schedule,
        ),
        selected_run=run_id,
    )


@router.get("/{p}/checks/{run}/steps", response_class=HTMLResponse)
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


@router.post("/{p}/checks")
def run_manual_check(
    request: Request,
    background: BackgroundTasks,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
):
    try:
        run_id = start_manual_check(engine, config, now, background.add_task)
    except ManualCheckConflict as exc:
        link = f"/{project}/checks/{exc.run_id}" if exc.run_id else f"/{project}/checks"
        from html import escape

        return HTMLResponse(
            f'<p>{escape(str(exc))}</p><a href="{link}">점검 보기</a>', status_code=409
        )
    return RedirectResponse(f"/{project}/checks/{run_id}", status_code=303)


@router.get("/{p}/checks/{run}/evidence/{evidence}/summary", response_class=HTMLResponse)
def evidence_summary_status(
    request: Request,
    run: str,
    evidence: str,
    project: ProjectDep,
    engine: EngineDep,
    now: NowDep,
):
    run_id = parse_id(run)
    drawer = build_drawer(engine, run_id, parse_id(evidence), now)
    if drawer is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "checks/_evidence_content.html",
        {"p": project, "run_id": run_id, "drawer": drawer, "summary_fragment": True},
    )


@router.post("/{p}/checks/{run}/evidence/{evidence}/summary", response_class=HTMLResponse)
def generate_evidence_summary(
    request: Request,
    run: str,
    evidence: str,
    project: ProjectDep,
    engine: EngineDep,
    now: NowDep,
    llm_factory: Annotated[LlmFactory, Depends(get_evidence_summary_factory)],
    retry: bool = False,
):
    run_id, number = parse_id(run), parse_id(evidence)
    if build_drawer(engine, run_id, number, now) is None:
        raise HTTPException(404)
    summarize_evidence(
        engine,
        format_evidence_id(number),
        model=get_setting().EVIDENCE_SUMMARY_MODEL,
        llm_factory=llm_factory,
        now=now,
        retry=retry,
    )
    return evidence_summary_status(request, run, evidence, project, engine, now)
