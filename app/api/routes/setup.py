"""점검 구성: 정의 조회와 점검 규칙 선택."""

from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.viewmodels.setup import (
    recent_links,
    rule_groups,
    service_details,
    services,
    yaml_text,
)
from api.viewmodels.shell import build_shell
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, render
from core.db import RuleSelectionStore
from service.checks.detectors.catalog import CATALOG

router = APIRouter(include_in_schema=False)


@router.get("/{p}/setup")
def setup_home(project: ProjectDep):
    return RedirectResponse(f"/{project}/setup/services", status_code=303)


def _context(engine, config, now, tab):
    return {
        "shell": build_shell(engine, config, now),
        "tab": tab,
        "service_names": services(config),
        "rule_count": len(CATALOG),
        "feature_count": len(config.api_scenarios),
    }


@router.get("/{p}/setup/services")
def setup_services(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    service: str | None = None,
):
    context = _context(engine, config, now, "services")
    names = context["service_names"]
    selected = service or (names[0] if names else None)
    if selected and selected not in names:
        raise HTTPException(404)
    return render(
        request,
        "setup/services.html",
        **context,
        selected=selected,
        definition=service_details(config, selected) if selected else None,
        target_yaml=yaml_text(
            {"core_paths": config.core_paths, "required_config": config.required_config}
        ),
    )


@router.get("/{p}/setup/rules")
def setup_rules(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    view: str = "stage",
    rule: str | None = None,
    saved: str | None = None,
):
    if view not in {"stage", "kind"}:
        raise HTTPException(400, "보기는 stage 또는 kind여야 합니다")
    groups, definitions = rule_groups(engine, view)
    selected = rule or "W02"
    if selected not in definitions:
        raise HTTPException(404)
    return render(
        request,
        "setup/rules.html",
        **_context(engine, config, now, "rules"),
        groups=groups,
        selected=definitions[selected],
        view=view,
        saved=saved,
        enabled_count=sum(r.enabled for r in definitions.values()),
        implemented_count=sum(r.available for r in definitions.values()),
        future_count=sum(not r.available for r in definitions.values()),
        recent=recent_links(engine, rule=selected),
    )


@router.post("/{p}/setup/rules")
async def save_rules(
    request: Request, project: ProjectDep, engine: EngineDep, config: ConfigDep, now: NowDep
):
    try:
        data = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as error:
        raise HTTPException(400, "UTF-8 형식의 입력이 필요합니다") from error
    enabled = set(data.get("enabled", []))
    try:
        RuleSelectionStore(engine).save(enabled, set(CATALOG), now)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    view = data.get("view", ["stage"])[0]
    if view not in {"stage", "kind"}:
        view = "stage"
    return RedirectResponse(f"/{project}/setup/rules?view={view}&saved=1", status_code=303)


@router.get("/{p}/setup/features")
def setup_features(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    feature: str | None = None,
):
    scenarios = {s.name: s for s in config.api_scenarios}
    selected = feature or next(iter(scenarios), None)
    if selected and selected not in scenarios:
        raise HTTPException(404)
    scenario = scenarios.get(selected)
    return render(
        request,
        "setup/features.html",
        **_context(engine, config, now, "features"),
        scenarios=scenarios.values(),
        selected=scenario,
        definition=yaml_text(scenario.model_dump(mode="json", exclude_none=True))
        if scenario
        else None,
        recent=recent_links(engine, feature=selected) if selected else [],
    )
