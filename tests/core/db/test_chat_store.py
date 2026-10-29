from datetime import UTC, datetime, timedelta

import pytest

from core.db import ChatStore, create_db_engine

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def test_chat_store_recent_order_and_limit(engine):
    store = ChatStore(engine)
    for i in range(12):
        store.add("c1", "user" if i % 2 == 0 else "assistant", f"m{i}", NOW + timedelta(seconds=i))

    turns = store.recent("c1", limit=10)

    assert [t.text for t in turns] == [f"m{i}" for i in range(2, 12)]
    assert turns[0].role == "user"


def test_conversations_isolated(engine):
    store = ChatStore(engine)
    store.add("a", "user", "A", NOW)
    store.add("b", "user", "B", NOW)

    assert [t.text for t in store.recent("a")] == ["A"]
    assert store.recent("none") == []


def test_cited_collects_ids_from_assistant_answers(engine):
    store = ChatStore(engine)
    answer = {
        "sentences": [
            {"text": "A", "kind": "fact", "evidence_ids": ["ev-0001"], "issue_ids": [3]},
            {"text": "B", "kind": "fact", "run_ids": [7], "issue_ids": [3]},
        ]
    }
    store.add("c1", "user", "q", NOW)
    store.add("c1", "assistant", "a", NOW, answer)
    store.add("other", "assistant", "a", NOW, {"sentences": [{"text": "x", "run_ids": [99]}]})

    assert store.cited("c1") == ({"ev-0001"}, {3}, {7})
    assert store.cited("none") == (set(), set(), set())
