from unittest.mock import patch

from core.db import ChatStore
from service.chat import ChatReply
from service.model.chat import AnswerSentence, ChatAnswer
from tests.api.seed import at


def test_panel_examples_and_context(client, engine):
    response = client.get("/dev/chat")
    assert response.status_code == 200
    assert "이번 주 긴급 이슈 몇 건?" in response.text
    assert "kg_chat=" in response.headers["set-cookie"]
    assert client.get("/other/chat").status_code == 404
    assert client.get("/dev/chat?issue=999").status_code == 404


def test_question_validation_does_not_call_agent(client):
    with patch("api.routes.chat_pages.answer_question") as answer:
        for question in ["", "a" * 501]:
            response = client.post("/dev/chat/messages", data={"question": question})
            assert "1~500자" in response.text
        answer.assert_not_called()


def test_reply_escapes_and_passes_project(client):
    reply = ChatReply(
        conversation_id="x",
        text="<script>alert(1)</script>",
        answer=ChatAnswer(
            sentences=[
                AnswerSentence(
                    text="<script>alert(1)</script>",
                    kind="interpretation",
                    issue_ids=[12],
                    run_ids=[1],
                )
            ]
        ),
    )
    with patch("api.routes.chat_pages.answer_question", return_value=reply) as answer:
        response = client.post("/dev/chat/messages", data={"question": "password=secret"})
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "password=secret" not in response.text
    assert "(추정)" in response.text
    assert "/dev/issues/12" in response.text
    assert answer.call_args.kwargs["project"] == "dev"


def test_evidence_must_have_been_cited_in_current_conversation(client, engine):
    client.get("/dev/chat")
    assert client.get("/dev/chat/evidence/ev-0001").status_code == 404
    cid = client.cookies.get("kg_chat")
    ChatStore(engine).add(
        cid, "assistant", "답변", at(1), {"sentences": [{"evidence_ids": ["ev-0001"]}]}
    )
    assert client.get("/dev/chat/evidence/ev-0001").status_code == 404


def test_list_filter_context_is_preserved_in_question(client):
    response = client.get("/dev/chat", params={"filters": "period=7d&severity=critical"})
    assert "period=7d" in response.text
    assert 'name="filters"' in response.text
    reply = ChatReply(conversation_id="x", text="답변", answer=ChatAnswer(sentences=[]))
    with patch("api.routes.chat_pages.answer_question", return_value=reply) as answer:
        client.post(
            "/dev/chat/messages",
            data={"question": "이 기간에는 몇 건?", "filters": "period=7d&severity=critical&ev=1"},
        )
    context = answer.call_args.kwargs["context"]
    assert "period=7d" in context and "severity=critical" in context
    assert "ev=1" not in context


def test_restored_answers_keep_citation_links(client, engine):
    client.get("/dev/chat")
    cid = client.cookies.get("kg_chat")
    ChatStore(engine).add(
        cid,
        "assistant",
        "답변",
        at(1),
        ChatAnswer(
            sentences=[AnswerSentence(text="저장된 답변", kind="fact", evidence_ids=["ev-0001"])]
        ).model_dump(),
    )
    response = client.get("/dev/chat")
    assert "/dev/chat/evidence/ev-0001" in response.text


def test_context_dividers_restore_switch_and_clear(client, engine):
    from tests.api.seed import make_run, open_incident

    run = make_run(engine)
    issue = open_incident(engine, run)
    client.get("/dev/chat")
    cid = client.cookies.get("kg_chat")
    ChatStore(engine).add(cid, "user", "old question", at(0))
    ChatStore(engine).add(
        cid, "assistant", "old answer", at(1), ChatAnswer(sentences=[]).model_dump()
    )
    response = client.get(f"/dev/chat?issue={issue}")
    assert f"맥락: 이슈 #{issue}" in response.text
    assert 'id="chat-pending-context"' in response.text
    ChatStore(engine).add(cid, "user", "issue question", at(2), {"_context": f"이슈 #{issue}"})
    ChatStore(engine).add(
        cid,
        "assistant",
        "issue answer",
        at(3),
        {**ChatAnswer(sentences=[]).model_dump(), "_context": f"이슈 #{issue}"},
    )
    response = client.get(f"/dev/chat?issue={issue}")
    assert response.status_code == 200
    assert response.text.count(f"맥락: 이슈 #{issue}") == 1
    assert 'id="chat-pending-context"' not in response.text
    response = client.get("/dev/chat")
    assert "맥락: 전체 이력" in response.text
    assert "old question" in response.text and "issue question" in response.text


def test_post_divider_only_for_different_stored_context(client, engine):
    from tests.api.seed import make_run, open_incident

    issue = open_incident(engine, make_run(engine))
    client.get("/dev/chat")
    cid = client.cookies.get("kg_chat")
    reply = ChatReply(conversation_id=cid, text="답변", answer=ChatAnswer(sentences=[]))
    with patch("api.routes.chat_pages.answer_question", return_value=reply):
        first = client.post("/dev/chat/messages", data={"question": "질문", "issue": str(issue)})
        assert f"맥락: 이슈 #{issue}" in first.text
        ChatStore(engine).add(cid, "user", "질문", at(1), {"_context": f"이슈 #{issue}"})
        same = client.post(
            "/dev/chat/messages", data={"question": "후속 질문", "issue": str(issue)}
        )
        assert 'role="separator"' not in same.text
        cleared = client.post("/dev/chat/messages", data={"question": "전체 이력 질문"})
        assert "맥락: 전체 이력" in cleared.text
