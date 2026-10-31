import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.deps import get_config, get_engine, get_llm_factory
from core.config import KubeGuardianConfig
from core.db import create_db_engine
from core.db.tables import ChatMessageRow
from main import create_app
from tests.service.agent.history_qa.fakes import ScriptedLLM

URL = "/api/v1/chat/messages"
ANSWER = json.dumps({"sentences": [{"text": "이력 없음", "kind": "guide"}]})


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _client(engine, script):
    app = create_app()
    llm = ScriptedLLM(script=script, calls=[], bound=[])
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_config] = lambda: KubeGuardianConfig(target={"namespace": "dev"})
    app.dependency_overrides[get_llm_factory] = lambda: lambda _m: llm
    return TestClient(app)


def _chat_rows(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(ChatMessageRow))


def test_post_message_ok(engine):
    res = _client(engine, ["끝", ANSWER]).post(URL, json={"question": "최근 이슈는?"})
    body = res.json()
    assert res.status_code == 200
    assert body["conversation_id"]
    assert body["answer"]["sentences"]


def test_post_message_too_long(engine):
    res = _client(engine, []).post(URL, json={"question": "가" * 501})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == 10001


def test_post_message_llm_failure(engine):
    res = _client(engine, [RuntimeError("down")]).post(URL, json={"question": "질문"})
    assert res.status_code == 502
    assert res.json()["error"]["code"] == 93001
    assert _chat_rows(engine) == 0


def test_invalid_conversation_id_rejected(engine):
    res = _client(engine, []).post(URL, json={"question": "질문", "conversation_id": "../x y"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == 10001
