"""이력 질의 패널: 저장된 이력만 읽는 HTML 인터페이스."""

import json
import re
import unicodedata
from typing import Annotated
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from api.deps import get_llm_factory
from api.timefmt import date_range
from api.web import ConfigDep, EngineDep, ProjectDep, templates
from core.db import ChatStore, CheckRunStore, EvidenceStore, IncidentStore
from core.db.tables import ChatMessageRow
from core.error.service_exception import ServiceException
from core.guard.redact import redact_mapping, redact_text
from service.agent.base import LlmFactory
from service.chat import answer_question, chat_context
from service.model.chat import ChatAnswer

router = APIRouter(include_in_schema=False)


def conversation(request: Request) -> str:
    value = request.cookies.get("kg_chat", "")
    return value if re.fullmatch(r"[a-f0-9]{32}", value) else uuid4().hex


def context_label(engine, issue: str = "", run: str = "") -> str:
    if issue:
        if not issue.isdecimal() or len(issue) > 10 or not IncidentStore(engine).get(int(issue)):
            raise HTTPException(404)
        return f"이슈 #{int(issue)}"
    if run:
        if not run.isdecimal() or len(run) > 10 or not CheckRunStore(engine).exists(int(run)):
            raise HTTPException(404)
        return f"점검 #{int(run)}"
    return "전체 이력"


# 값이 정해진 필터는 화면(checks, issues)이 쓰는 값만 받는다. 벗어난 값은 버린다.
FILTER_CHOICES = {
    "period": {"all", "today", "1d", "7d", "30d"},
    "severity": {"critical", "warning", "info", "tip", "calm", "crit", "warn", "risk"},
    "relation": {"opened", "continued", "changed", "seen", "resolved", "none"},
    "trigger": {"schedule", "ui", "check", "investigate"},
    "status": {"open", "resolved", "running", "done", "unknown", "halted", "failed"},
}
MAX_FILTER_Q = 100


def flatten_text(raw: str) -> str:
    """제어·서식 문자를 없애고 공백을 하나로 합친 뒤 마스킹한다. 마스킹은 평탄화한 문자열에 한다."""
    kept = "".join(c for c in raw if unicodedata.category(c) != "Cf")
    flat = re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\s]+", " ", kept).strip()
    return redact_text(flat)


def filter_values(value: str) -> dict[str, str]:
    """필터 쿼리를 허용 키·값만 남긴 딕셔너리로 정리한다. 자유 문자열 q는 마스킹 뒤 자른다."""
    pairs = parse_qs(value[:2000])
    result: dict[str, str] = {}
    for key, values in sorted(pairs.items()):
        raw = values[0].strip()
        if key in FILTER_CHOICES:
            if raw in FILTER_CHOICES[key]:
                result[key] = raw
        elif key == "days":
            if raw.isascii() and raw.isdecimal() and 0 < int(raw) <= 365:
                result[key] = str(int(raw))
        elif key in ("from", "to"):
            result[key] = raw  # 아래에서 날짜 범위로 한꺼번에 검증한다
        elif key == "q":
            text = flatten_text(raw)
            if text:
                result[key] = text[:MAX_FILTER_Q]
    start, end = date_range(result.pop("from", ""), result.pop("to", ""))
    result |= {k: v for k, v in (("from", start), ("to", end)) if v}
    return result


def filter_context(value: str) -> str:
    values = filter_values(value)
    start, end = values.pop("from", ""), values.pop("to", "")
    parts = [f"{k}={v}" for k, v in values.items()]
    if start or end:
        parts.append(f"기간 {start}~{end}")
    return ", ".join(parts)


@router.get("/{p}/chat")
def chat_panel(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    issue: str = "",
    run: str = "",
    filters: str = "",
):
    return render_panel(request, project, engine, conversation(request), issue, run, filters)


@router.post("/{p}/chat/new")
def new_chat(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    issue: str = "",
    run: str = "",
    filters: str = "",
):
    return render_panel(request, project, engine, uuid4().hex, issue, run, filters)


def render_panel(request, project, engine, cid, issue, run, filters):
    with Session(engine) as session:
        stored = list(
            session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.conversation_id == cid)
                .order_by(ChatMessageRow.id.desc())
                .limit(20)
            )
        )
    filter_text = filter_context(filters)
    label = chat_context(
        context_label(engine, issue, run) + (" · 필터: " + filter_text if filter_text else "")
    )
    turns = []
    previous_context = "전체 이력"
    for turn in reversed(stored):
        metadata = turn.answer or {}
        context = chat_context(metadata.get("_context", "전체 이력"))
        turns.append(
            {
                "role": turn.role,
                "text": turn.text,
                "answer": ChatAnswer.model_validate(metadata)
                if turn.role == "assistant" and "sentences" in metadata
                else None,
                "context": context,
                "context_changed": context != previous_context,
            }
        )
        previous_context = context
    response = templates.TemplateResponse(
        request,
        "chat/panel.html",
        {
            "p": project,
            "context": label,
            "pending_context": bool(turns and previous_context != label),
            "filters": filters[:2000],
            "issue": issue,
            "run": run,
            "turns": turns,
        },
    )
    response.set_cookie("kg_chat", cid, httponly=True, samesite="strict")
    return response


@router.post("/{p}/chat/messages")
async def chat_message(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    llm_factory: Annotated[LlmFactory, Depends(get_llm_factory)],
):
    body = await request.body()
    if len(body) > 16000:
        raise HTTPException(400)
    form = parse_qs(body.decode("utf-8", errors="replace"))
    question = form.get("question", [""])[0].strip()
    issue, run = form.get("issue", [""])[0], form.get("run", [""])[0]
    scope = context_label(engine, issue, run)
    filter_dict = filter_values(form.get("filters", [""])[0])
    filters = filter_context(form.get("filters", [""])[0])
    label = chat_context(scope + (" · 필터: " + filters if filters else ""))
    screen = {"화면": scope, "필터": filter_dict} if scope != "전체 이력" or filter_dict else None
    cid = conversation(request)
    with Session(engine) as session:
        previous = session.scalar(
            select(ChatMessageRow.answer)
            .where(ChatMessageRow.conversation_id == cid)
            .order_by(ChatMessageRow.id.desc())
            .limit(1)
        )
    previous_context = chat_context((previous or {}).get("_context", "전체 이력"))
    error, reply = None, None
    if not question or len(question) > 500:
        error = "질문은 1~500자로 입력하세요."
    else:
        try:
            reply = await run_in_threadpool(
                answer_question,
                question,
                engine=engine,
                config=config,
                llm_factory=llm_factory,
                conversation_id=cid,
                project=project,
                context=label,
                screen=screen,
            )
        except ServiceException:
            error = "답변을 만들지 못했습니다. 잠시 후 다시 질문해 주세요."
    response = templates.TemplateResponse(
        request,
        "chat/reply.html",
        {
            "p": project,
            "question": redact_text(question),
            "reply": reply,
            "error": error,
            "context": label,
            "context_changed": previous_context != label,
        },
    )
    response.set_cookie("kg_chat", cid, httponly=True, samesite="strict")
    return response


@router.get("/{p}/chat/evidence/{ev}")
def chat_evidence(request: Request, project: ProjectDep, engine: EngineDep, ev: str):
    if ev not in ChatStore(engine).cited(conversation(request))[0]:
        raise HTTPException(404)
    evidence = EvidenceStore(engine).get(ev)
    if evidence is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "chat/evidence.html",
        {
            "subject": redact_text(evidence.subject),
            "source": redact_text(evidence.source),
            "raw": json.dumps(redact_mapping(evidence.data), ensure_ascii=False, indent=2),
        },
    )
