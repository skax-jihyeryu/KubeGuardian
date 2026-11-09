"""화면 공통: 템플릿 환경(autoescape), 프로젝트 확인, 틀(GNB·띠) 값."""

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import Engine

from api import timefmt
from api.deps import get_config, get_engine, get_now
from api.viewmodels.shell import Shell
from core.config import KubeGuardianConfig
from core.guard.redact import redact_text

API_DIR = Path(__file__).parent
STATIC_DIR = API_DIR / "static"

# autoescape를 켠다. 로그·LLM 문자열을 |safe로 내보내지 않는다 (Review Focus 1)
_env = Environment(loader=FileSystemLoader(API_DIR / "templates"), autoescape=True)
_env.filters.update(
    hm=timefmt.hm,
    hms=timefmt.hms,
    md_hm=timefmt.md_hm,
    answer_minutes=timefmt.answer_minutes,
    redact=redact_text,
)
templates = Jinja2Templates(env=_env)

_ID = re.compile(r"[1-9]\d{0,9}")  # SQLite 정수 범위를 넘는 값은 조회하기 전에 없는 것으로 본다


def require_project(p: str, config: Annotated[KubeGuardianConfig, Depends(get_config)]) -> str:
    """주소의 프로젝트는 설정의 project.id 하나뿐이다."""
    if p != config.project.id:
        raise HTTPException(404)
    return p


EngineDep = Annotated[Engine, Depends(get_engine)]
ConfigDep = Annotated[KubeGuardianConfig, Depends(get_config)]
NowDep = Annotated[datetime, Depends(get_now)]
ProjectDep = Annotated[str, Depends(require_project)]


def parse_id(value: str) -> int:
    """숫자가 아니거나 범위 밖이면 없는 것(404)으로 본다."""
    if not _ID.fullmatch(value):
        raise HTTPException(404)
    return int(value)


def render(request: Request, name: str, shell: Shell, **context: Any):
    return templates.TemplateResponse(
        request, name, {"shell": shell, "p": shell.project, **context}
    )
