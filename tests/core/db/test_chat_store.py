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
