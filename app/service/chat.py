"""이력 질의 채팅 한 턴: 질문 검증·마스킹 → 에이전트 → 답변 검증·마스킹 → 안내 문장 덧붙이기."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import ChatStore, CheckRunStore, EvidenceStore, IncidentStore
from core.error.service_exception import ErrorCode, ServiceException
from core.guard.budget import BudgetTracker
from core.guard.redact import redact_text
from core.guard.validate import validate_answer
from service.agent.base import LlmFactory, RunContext
from service.agent.history_qa.v1 import ReadOnlyEvidence
from service.agent.history_qa.v1.agent import HistoryQaV1
from service.agent.history_qa.v1.config import SPEC
from service.agent.history_qa.v1.tools import KNOWLEDGE_DIR, build_history_tools
from service.model.chat import AnswerSentence, ChatAnswer, ChatRequest
from service.notify.messages import DISPLAY_TZ

DEFAULT_PROJECT = "dev"
MAX_QUESTION = 500
HISTORY_TURNS = 10
CURRENT_STATE_GUIDE = (
    "저장된 이력만으로는 지금 상태를 알 수 없습니다. "
    "상단의 지금 점검을 누르면 점검 상세에서 진행 과정과 결과를 볼 수 있습니다."
)
INVESTIGATE_GUIDE = "증상을 적어 조사를 요청하려면 Slack에서 /kg investigate를 쓰세요."
PARTIAL_GUIDE = "도구 호출 상한에 닿아 일부 이력만 확인했습니다."


class ChatReply(BaseModel):
    conversation_id: str
    answer: ChatAnswer
    text: str


def _when(at: datetime) -> str:
    return at.astimezone(DISPLAY_TZ).strftime("%m/%d %H:%M")


def _guide(text: str, **ids) -> AnswerSentence:
    return AnswerSentence(text=text, kind="guide", **ids)


def answer_question(
    question: str,
    *,
    engine: Engine,
    config: KubeGuardianConfig,
    llm_factory: LlmFactory,
    conversation_id: str | None = None,
    project: str = DEFAULT_PROJECT,
    knowledge_dir: Path = KNOWLEDGE_DIR,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ChatReply:
    if len(question) > MAX_QUESTION:
        raise ServiceException(ErrorCode.VALIDATION_ERROR, "질문은 500자 이내")
    conversation_id = conversation_id or uuid4().hex[:12]

    chat = ChatStore(engine)
    history = chat.recent(conversation_id, HISTORY_TURNS)
    masked = redact_text(question)

    ctx = RunContext(
        run_id=conversation_id,
        namespace=config.target.namespace,
        evidence=ReadOnlyEvidence(),
        budget=BudgetTracker(SPEC.budget),
        now=now,
    )
    agent = HistoryQaV1(llm_factory, build_history_tools(engine, knowledge_dir), ctx)
    result = agent.run(ChatRequest(question=masked, history=history))
    if result.status == "failed":
        raise ServiceException(ErrorCode.LLM_CALL_FAILED, result.error)

    runs = CheckRunStore(engine)
    # 인용은 이번 턴에 본 번호이거나 이 대화의 이전 답변이 이미 인용한 번호이고 DB에도 있어야 한다.
    prev_ev, prev_issues, prev_runs = chat.cited(conversation_id)
    seen_ev = agent.seen.evidence | prev_ev
    seen_issues = agent.seen.issues | prev_issues
    seen_runs = agent.seen.runs | prev_runs
    answer = validate_answer(
        result.answer,
        evidence=lambda i: i in seen_ev and EvidenceStore(engine).get(i) is not None,
        issue=lambda i: i in seen_issues and IncidentStore(engine).get(i) is not None,
        run=lambda i: i in seen_runs and runs.exists(i),
    )
    sentences = [s.model_copy(update={"text": redact_text(s.text)}) for s in answer.sentences]

    if answer.current_state:
        front = [_guide(CURRENT_STATE_GUIDE)]
        if latest := runs.latest_run():
            run_id, started_at = latest
            front.append(
                _guide(
                    f"마지막 점검: {_when(started_at)} /p/{project}/checks/{run_id}",
                    run_ids=[run_id],
                )
            )
        sentences = front + sentences
    if answer.wants_investigation:
        sentences.append(_guide(INVESTIGATE_GUIDE))
    if answer.partial:
        sentences.append(_guide(PARTIAL_GUIDE))

    answer = answer.model_copy(update={"sentences": sentences})
    text = format_answer(answer)
    chat.add(conversation_id, "user", masked, now())
    chat.add(conversation_id, "assistant", text, now(), answer.model_dump())
    return ChatReply(conversation_id=conversation_id, answer=answer, text=text)


def format_answer(answer: ChatAnswer) -> str:
    """문장을 공백으로 잇고, 각 문장 뒤에 `[근거 ev-0001]` `[이슈 #12]` `[점검 #340]`을 붙인다."""
    parts = []
    for s in answer.sentences:
        marks = [
            *(f"[근거 {i}]" for i in s.evidence_ids),
            *(f"[이슈 #{i}]" for i in s.issue_ids),
            *(f"[점검 #{i}]" for i in s.run_ids),
        ]
        parts.append(" ".join([s.text, *marks]))
    return " ".join(parts)
