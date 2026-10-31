from datetime import UTC, datetime

import pytest

from core.db import EvidenceStore, create_db_engine
from evidence_db import agent_evidence_ids, load_evidence
from service.model import Evidence


def test_agent_evidence_ids_only_tools_steps():
    steps = [
        {"node": "plan", "evidence_ids": ["ev-0009"]},
        {"node": "tools", "evidence_ids": ["ev-0001", "ev-0002"]},
        {"node": "tools", "evidence_ids": ["ev-0002", "ev-0003"]},
        {"node": "validate"},
    ]

    assert agent_evidence_ids(steps) == ["ev-0001", "ev-0002", "ev-0003"]


def test_load_evidence_roundtrip(tmp_path):
    db = tmp_path / "check.db"
    store = EvidenceStore(create_db_engine(str(db)))
    ev = Evidence(
        kind="resource",
        subject="dev/Pod/a",
        collected_at=datetime.now(UTC),
        source="GET /x",
        data={"status": 500},
    )
    id1 = store.add(ev)
    store.add(ev)

    result = load_evidence(db, [id1, "ev-0099"])

    assert list(result) == [id1]
    assert result[id1] == {
        "id": id1,
        "kind": "resource",
        "subject": "dev/Pod/a",
        "data": {"status": 500},
    }
    assert isinstance(result[id1]["data"], dict)


def test_load_evidence_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_evidence(tmp_path / "none.db", ["ev-0001"])
