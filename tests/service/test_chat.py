import json
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import KubeGuardianConfig
from core.db import CheckRunStore, IncidentStore, create_db_engine
from core.db.tables import ChatMessageRow
from core.error.service_exception import ErrorCode, ServiceException
from service.chat import (
    CURRENT_STATE_GUIDE,
    INVESTIGATE_GUIDE,
    PARTIAL_GUIDE,
    answer_question,
    format_answer,
)
from service.model.chat import AnswerSentence, ChatAnswer
from tests.service.agent.history_qa.fakes import ScriptedLLM

CONFIG = KubeGuardianConfig(target={"namespace": "dev"})
NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _answer(sentences=None, **flags):
    sentences = sentences if sentences is not None else [{"text": "이력 없음", "kind": "guide"}]
    return json.dumps({"sentences": sentences, **flags})


def _ask(engine, script, question="질문", **kw):
    llm = ScriptedLLM(script=script, calls=[], bound=[])
    reply = answer_question(
        question, engine=engine, config=CONFIG, llm_factory=lambda _m: llm, **kw
    )
    return reply, llm


def test_too_long_rejected(engine):
    llm = ScriptedLLM(script=[], calls=[], bound=[])
    with pytest.raises(ServiceException) as e:
        answer_question("가" * 501, engine=engine, config=CONFIG, llm_factory=lambda _m: llm)
    assert e.value.error_code is ErrorCode.VALIDATION_ERROR
    assert llm.calls == []


def test_question_masked_before_llm(engine):
    _, llm = _ask(engine, ["끝", _answer()], question="password=hunter2 인데 왜?")
    assert all("hunter2" not in str(m.content) for call in llm.calls for m in call)


def test_masked_question_longer_than_limit_reaches_llm(engine):
    question = "pass=a " * 71 + "abc"
    assert len(question) == 500
    _, llm = _ask(engine, ["끝", _answer()], question=question)
    sent = str(llm.calls[0][-1].content)
    assert len(sent) > 500
    assert "pass=a" not in sent


def test_answer_masked(engine):
    s = [{"text": "설정은 token=abc123secret 입니다", "kind": "guide"}]
    reply, _ = _ask(engine, ["끝", _answer(s)])
    assert "abc123secret" not in reply.text
    assert "abc123secret" not in reply.answer.sentences[0].text


def test_current_state_guide_with_last_run(engine):
    CheckRunStore(engine).create("schedule", datetime(2026, 10, 20, 1, 0, tzinfo=UTC))
    reply, _ = _ask(engine, ["끝", _answer(current_state=True)], now=lambda: NOW)
    texts = [s.text for s in reply.answer.sentences]
    assert texts[0] == CURRENT_STATE_GUIDE
    assert reply.answer.sentences[0].kind == "guide"
    assert texts[1] == "마지막 점검: 10/20 10:00 /p/dev/checks/1"
    assert reply.answer.sentences[1].run_ids == [1]


def test_last_run_line_shows_date_when_old(engine):
    CheckRunStore(engine).create("schedule", NOW - timedelta(days=3))
    reply, _ = _ask(engine, ["끝", _answer(current_state=True)], now=lambda: NOW)
    assert reply.answer.sentences[1].text == "마지막 점검: 10/17 10:00 /p/dev/checks/1"


def test_current_state_guide_empty_db(engine):
    reply, _ = _ask(engine, ["끝", _answer([], current_state=True)])
    assert [s.text for s in reply.answer.sentences] == [CURRENT_STATE_GUIDE]
    assert "/p/" not in reply.text


def test_investigate_guide_appended(engine):
    reply, _ = _ask(engine, ["끝", _answer(wants_investigation=True)])
    assert reply.answer.sentences[-1].text == INVESTIGATE_GUIDE


def test_partial_guide_appended(engine):
    calls = [
        AIMessage(content="", tool_calls=[{"name": "list_runs", "args": {}, "id": f"c{i}"}])
        for i in range(8)
    ]
    reply, _ = _ask(engine, [*calls, _answer(wants_investigation=True)])
    texts = [s.text for s in reply.answer.sentences]
    assert texts[-2:] == [INVESTIGATE_GUIDE, PARTIAL_GUIDE]
    assert reply.answer.partial is True


def test_llm_failure_raises(engine):
    with pytest.raises(ServiceException) as e:
        _ask(engine, [RuntimeError("down")])
    assert e.value.error_code is ErrorCode.LLM_CALL_FAILED


def test_conversation_id_generated_and_kept(engine):
    reply, _ = _ask(engine, ["끝", _answer()])
    assert len(reply.conversation_id) == 12
    kept, _ = _ask(engine, ["끝", _answer()], conversation_id="abc")
    assert kept.conversation_id == "abc"


def test_format_answer():
    answer = ChatAnswer(
        sentences=[
            AnswerSentence(text="A", kind="fact", evidence_ids=["ev-0001"], issue_ids=[12]),
            AnswerSentence(text="B", kind="fact", run_ids=[340]),
        ]
    )
    assert format_answer(answer) == "A [근거 ev-0001] [이슈 #12] B [점검 #340]"


def _messages(engine):
    with Session(engine) as s:
        return list(s.scalars(select(ChatMessageRow).order_by(ChatMessageRow.id)))


def test_second_question_gets_history(engine):
    first, _ = _ask(engine, ["끝", _answer([{"text": "첫 답", "kind": "guide"}])], "첫 질문")
    _, llm = _ask(engine, ["끝", _answer()], "둘째", conversation_id=first.conversation_id)

    sent = [str(m.content) for m in llm.calls[0]]
    assert "첫 질문" in sent
    assert any("첫 답" in c for c in sent)


def test_saved_text_masked(engine):
    _ask(engine, ["끝", _answer()], question="password=hunter2 인데 왜?")
    rows = _messages(engine)
    assert [r.role for r in rows] == ["user", "assistant"]
    assert all("hunter2" not in r.text for r in rows)
    assert rows[1].answer is not None


def test_failure_not_saved(engine):
    with pytest.raises(ServiceException):
        _ask(engine, [RuntimeError("down")])
    assert _messages(engine) == []


def _cite_issue(issue_id=1):
    s = [{"text": "llm-gateway 장애가 있었습니다.", "kind": "fact", "issue_ids": [issue_id]}]
    return _answer(s)


def _open_issue(engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    return IncidentStore(engine).open("fp", {"services": ["web"]}, "high", run_id, NOW)


def _list_issues_call():
    return AIMessage(content="", tool_calls=[{"name": "list_issues", "args": {}, "id": "c1"}])


def test_unseen_existing_issue_citation_becomes_estimate(engine):
    _open_issue(engine)
    reply, _ = _ask(engine, ["끝", _cite_issue()])
    sentence = reply.answer.sentences[0]
    assert sentence.text.endswith(" (추정)")
    assert sentence.issue_ids == []


def test_seen_issue_citation_kept(engine):
    issue_id = _open_issue(engine)
    reply, _ = _ask(engine, [_list_issues_call(), "끝", _cite_issue(issue_id)])
    sentence = reply.answer.sentences[0]
    assert not sentence.text.endswith("(추정)")
    assert sentence.issue_ids == [issue_id]


def test_previously_cited_id_kept_in_next_turn(engine):
    issue_id = _open_issue(engine)
    first, _ = _ask(engine, [_list_issues_call(), "끝", _cite_issue(issue_id)])
    second, _ = _ask(engine, ["끝", _cite_issue(issue_id)], conversation_id=first.conversation_id)
    assert second.answer.sentences[0].issue_ids == [issue_id]
    other, _ = _ask(engine, ["끝", _cite_issue(issue_id)], conversation_id="fresh")
    assert other.answer.sentences[0].issue_ids == []


def test_seen_but_missing_in_db_removed(engine):
    # 도구 결과에 나온 번호라도 DB에 없으면(예: 그 사이 삭제) 지운다.
    issue_id = _open_issue(engine)
    llm = ScriptedLLM(script=[_list_issues_call(), "끝", _cite_issue(issue_id)], calls=[], bound=[])
    from unittest.mock import patch

    with patch("service.chat.IncidentStore.get", return_value=None):
        reply = answer_question("q", engine=engine, config=CONFIG, llm_factory=lambda _m: llm)
    assert reply.answer.sentences[0].issue_ids == []
