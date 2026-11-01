from datetime import UTC, datetime

import pytest

from service.agent import registry
from service.agent.base import AgentResult
from service.agent.history_qa.v1 import ReadOnlyEvidence
from service.agent.history_qa.v1.agent import HistoryQaV1
from service.model import Evidence


def test_registered():
    assert registry.get("history_qa:v1") is HistoryQaV1
    assert HistoryQaV1.spec.models == {"answer": "gpt-6.1-sol"}
    assert HistoryQaV1.spec.budget.total_tool_calls == 8


def test_read_only_evidence_refuses_writes():
    ev = Evidence(
        kind="resource",
        subject="dev/Pod/a",
        collected_at=datetime.now(UTC),
        source="GET /x",
        data={},
    )
    with pytest.raises(RuntimeError):
        ReadOnlyEvidence().add(ev)


def test_agent_result_answer_defaults_none():
    assert AgentResult(status="done").answer is None
