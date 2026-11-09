"""이력 질의 패널: 저장된 이력만 읽는 HTML 인터페이스."""

import json
import re
import unicodedata
from datetime import timedelta
from typing import Annotated
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from api.deps import get_llm_factory
from api.timefmt import date_range, date_window, md_hm
from api.web import ConfigDep, EngineDep, NowDep, ProjectDep, templates
from core.db import ChatStore, CheckRunStore, EvidenceStore, IncidentStore
from core.db.tables import ChatMessageRow
from core.error.service_exception import ServiceException
from core.guard.redact import redact_mapping, redact_text
from service.agent.base import LlmFactory
from service.chat import answer_question, chat_context
from service.model.chat import ChatAnswer, HistoryWindows

router = APIRouter(include_in_schema=False)


def conversation(request: Request) -> str:
    value = request.cookies.get("kg_chat", "")
    return value if CHAT_ID.fullmatch(value) else uuid4().hex


CHAT_ID = re.compile(r"[a-f0-9]{32}")
HISTORY_LIMIT = 20
HISTORY_COOKIE = "kg_chat_history"


def history_ids(request: Request) -> list[str]:
    raw = request.cookies.get(HISTORY_COOKIE, "")[: HISTORY_LIMIT * 33]
    return list(dict.fromkeys(cid for cid in raw.split(".") if CHAT_ID.fullmatch(cid)))


def remember_chat(request: Request, response, cid: str) -> None:
    ids = list(dict.fromkeys([cid, *history_ids(request)]))[:HISTORY_LIMIT]
    response.set_cookie(
        HISTORY_COOKIE, ".".join(ids), max_age=30 * 86400, httponly=True, samesite="strict"
    )


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
CHAT_PERIODS = {"1d": "최근 24시간", "7d": "최근 7일", "30d": "최근 30일"}
CHAT_PERIOD_TARGETS = {"checks": "점검 이력", "issues": "이슈"}
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


def filter_context(values: dict[str, str]) -> str:
    start, end = values.get("from", ""), values.get("to", "")
    parts = [f"{k}={v}" for k, v in values.items() if k not in ("from", "to")]
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
    view: str = "",
):
    return render_panel(request, project, engine, conversation(request), issue, run, filters, view)


@router.post("/{p}/chat/new")
def new_chat(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    issue: str = "",
    run: str = "",
    filters: str = "",
    view: str = "",
):
    return render_panel(request, project, engine, uuid4().hex, issue, run, filters, view)


@router.get("/{p}/chat/mentions")
def chat_mentions(request: Request, project: ProjectDep, engine: EngineDep, q: str = ""):
    term = q[:100].strip().removeprefix("@").strip().casefold()
    kind = ""
    for prefix, record_type in (("이슈", "issue"), ("점검", "run")):
        if term.startswith(prefix):
            kind, term = record_type, term.removeprefix(prefix).strip()
            break
    term = term.removeprefix("#").strip()
    record_id = int(term) if term.isascii() and term.isdecimal() and len(term) <= 10 else None
    records = []
    if kind != "run":
        store = IncidentStore(engine)
        issues = (
            [store.get(record_id)]
            if record_id is not None
            else store.list_incidents(limit=1000 if term else 6)
        )
        for issue in issues:
            if issue is None:
                continue
            detail = redact_text(", ".join(issue.features or issue.services))
            if term and record_id is None and term not in detail.casefold():
                continue
            records.append(("issue", issue.id, "이슈", detail or md_hm(issue.opened_at)))
            if len(records) == 6:
                break
    if kind != "issue":
        store = CheckRunStore(engine)
        if record_id is not None:
            run = store.get_run(record_id)
            runs = [(record_id, run[0])] if run else []
        else:
            runs = (
                [(run_id, at) for run_id, at, _, _ in store.list_runs(limit=6)] if not term else []
            )
        records.extend(("run", run_id, "점검", md_hm(at)) for run_id, at in runs)
    return templates.TemplateResponse(request, "chat/mentions.html", {"records": records})


@router.get("/{p}/chat/history")
def chat_history(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    issue: str = "",
    run: str = "",
    filters: str = "",
    view: str = "",
):
    ids = history_ids(request)
    first_turn_ids = (
        select(func.min(ChatMessageRow.id))
        .where(ChatMessageRow.conversation_id.in_(ids), ChatMessageRow.role == "user")
        .group_by(ChatMessageRow.conversation_id)
    )
    with Session(engine) as session:
        titles = {
            row.conversation_id: redact_text(row.text)[:80]
            for row in session.scalars(
                select(ChatMessageRow).where(ChatMessageRow.id.in_(first_turn_ids))
            )
        }
    return templates.TemplateResponse(
        request,
        "chat/history.html",
        {
            "p": project,
            "history": [(cid, titles[cid]) for cid in ids if cid in titles],
            "current": conversation(request),
            "params": {"issue": issue, "run": run, "filters": filters[:2000], "view": view},
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{p}/chat/resume")
async def resume_chat(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    issue: str = "",
    run: str = "",
    filters: str = "",
    view: str = "",
):
    body = await request.body()
    if len(body) > 128:
        raise HTTPException(400)
    cid = parse_qs(body.decode("utf-8", errors="replace")).get("cid", [""])[0]
    if cid not in history_ids(request):
        raise HTTPException(404)
    return render_panel(request, project, engine, cid, issue, run, filters, view)


def render_panel(request, project, engine, cid, issue, run, filters, view):
    view = view if view in CHAT_PERIOD_TARGETS else ""
    with Session(engine) as session:
        stored = list(
            session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.conversation_id == cid)
                .order_by(ChatMessageRow.id.desc())
                .limit(20)
            )
        )
    filter_text = filter_context(filter_values(filters))
    scope = context_label(engine, issue, run)
    label = chat_context(scope + (" · 필터: " + filter_text if filter_text else ""))
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
            "filter_text": filter_text,
            "filter_view": view,
            "cid": cid,
            "scope": scope,
            "periods": CHAT_PERIODS,
            "period_targets": CHAT_PERIOD_TARGETS,
            "pending_context": bool(turns and previous_context != label),
            "filters": filters[:2000],
            "issue": issue,
            "run": run,
            "turns": turns,
        },
    )
    response.set_cookie("kg_chat", cid, httponly=True, samesite="strict")
    if stored:
        remember_chat(request, response, cid)
    return response


def chat_period(form: dict[str, list[str]], prefix: str) -> tuple[dict[str, str], str]:
    mode = form.get(f"{prefix}_period", [""])[0]
    if not mode:
        return {}, ""
    if mode in CHAT_PERIODS:
        return {"period": mode}, CHAT_PERIODS[mode]
    if mode == "custom":
        start, end = date_range(
            form.get(f"{prefix}_from", [""])[0], form.get(f"{prefix}_to", [""])[0]
        )
        if start and end:
            return {"from": start, "to": end}, f"{start}~{end}"
    raise ValueError("invalid period")


@router.post("/{p}/chat/messages")
async def chat_message(
    request: Request,
    project: ProjectDep,
    engine: EngineDep,
    config: ConfigDep,
    now: NowDep,
    llm_factory: Annotated[LlmFactory, Depends(get_llm_factory)],
):
    body = await request.body()
    if len(body) > 16000:
        raise HTTPException(400)
    form = parse_qs(body.decode("utf-8", errors="replace"))
    question = form.get("question", [""])[0].strip()
    issues = dict.fromkeys(form.get("issue", []))
    runs = dict.fromkeys(form.get("run", []))
    selected = [context_label(engine, issue=issue) for issue in issues]
    selected.extend(context_label(engine, run=run) for run in runs)
    scope = ", ".join(selected) or "전체 이력"
    filter_dict = filter_values(form.get("filters", [""])[0])
    windows: HistoryWindows = {}
    periods = []
    period_labels = []
    range_error = False
    try:
        for prefix, tool in (("checks", "list_runs"), ("issues", "list_issues")):
            period, description = chat_period(form, prefix)
            if not period:
                continue
            target = CHAT_PERIOD_TARGETS[prefix]
            if relative := period.get("period"):
                since, until = now - timedelta(days=int(relative[:-1])), now
            else:
                since, until = date_window(period["from"], period["to"])
                assert since is not None and until is not None
            windows[tool] = (since, until)
            periods.append({"대상": target, "since": since.isoformat(), "until": until.isoformat()})
            period_labels.append(f"{target} 기간 {description}")
        # 열린 구버전 패널에서 전송한 단일 기간 폼과 호환한다.
        legacy, description = chat_period(form, "chat") if not periods else ({}, "")
        if legacy:
            target = form.get("chat_period_target", [""])[0]
            if target:
                if target not in CHAT_PERIOD_TARGETS:
                    raise ValueError("invalid period target")
                legacy["대상"] = CHAT_PERIOD_TARGETS[target]
            period_labels.append(f"{legacy.get('대상', '')} 기간 {description}".strip())
    except ValueError:
        range_error = True
        legacy = {}

    view = form.get("view", [""])[0]
    if view not in CHAT_PERIOD_TARGETS:
        view = ""
    if period_labels and (
        not view
        or (view == "checks" and "list_runs" in windows)
        or (view == "issues" and "list_issues" in windows)
    ):
        for key in ("from", "to", "period", "days"):
            filter_dict.pop(key, None)
    filters = filter_context(filter_dict)
    label = " · ".join([scope, *period_labels])
    if filters:
        label += " · 필터: " + filters
    label = chat_context(label)
    screen = {"화면": scope, "필터": filter_dict}
    if view and filter_dict:
        screen["목록 대상"] = CHAT_PERIOD_TARGETS[view]
    if periods or legacy:
        screen["추가 이력 기간"] = periods or legacy
    active = conversation(request)
    cid = form.get("cid", [active])[0]
    if cid != active and cid not in history_ids(request):
        raise HTTPException(404)
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
    elif range_error:
        error = "조회 기간을 확인하세요. 시작일과 종료일을 순서대로 선택해 주세요."
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
                history_windows=windows,
                now=lambda: now,
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
    if reply is not None:
        remember_chat(request, response, cid)
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
