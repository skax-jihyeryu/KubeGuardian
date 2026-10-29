import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage

from core.config import KubeGuardianConfig
from core.db import CheckRunStore, create_db_engine
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


def test_answer_masked(engine):
    s = [{"text": "설정은 token=abc123secret 입니다", "kind": "guide"}]
    reply, _ = _ask(engine, ["끝", _answer(s)])
    assert "abc123secret" not in reply.text
    assert "abc123secret" not in reply.answer.sentences[0].text


def test_current_state_guide_with_last_run(engine):
    CheckRunStore(engine).create("schedule", datetime(2026, 10, 20, 1, 0, tzinfo=UTC))
    reply, _ = _ask(engine, ["끝", _answer(current_state=True)])
    texts = [s.text for s in reply.answer.sentences]
    assert texts[0] == CURRENT_STATE_GUIDE
    assert reply.answer.sentences[0].kind == "guide"
    assert texts[1] == "마지막 점검: 10:00 /p/dev/checks/1"
    assert reply.answer.sentences[1].run_ids == [1]


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
