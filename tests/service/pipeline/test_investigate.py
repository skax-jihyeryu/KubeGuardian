import json

import pytest
from langchain_core.messages import AIMessage

from core.db import EvidenceStore, create_db_engine
from service.pipeline.check import run_check
from service.pipeline.investigate import build_request, run_investigation
from tests.service.agent.investigator.test_v1_graph import ScriptedLLM
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _reader(case=None):
    objects, failed = raw_cluster(case)
    return FixtureReader(objects, failed)


def test_gate_c_builds_no_request(engine):
    out = run_check(_reader(), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "C"
    assert build_request(out, CONFIG, trigger="check") is None


def test_gate_a_request_carries_signal_board_and_topology(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    req = build_request(out, CONFIG, trigger="check")

    assert req.gate_path == "A"
    assert any(s.rule_id == "S04" for ss in req.signal_board.by_service.values() for s in ss)
    assert any(e.status == "unresolved" for e in req.topology.edges)


def test_run_investigation_uses_registered_agent_and_stores_evidence(engine):
    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)
    triage = json.dumps(
        {
            "issues": [
                {
                    "rank": 1,
                    "title": "주소 설정 오류",
                    "impact": "core_path_broken",
                    "confidence": "high",
                }
            ],
            "runbook": "service-unreachable",
        }
    )
    s04 = next(s for ss in out.board.by_service.values() for s in ss if s.rule_id == "S04")
    report = json.dumps(
        {
            "conclusion": "gateway 주소 env가 localhost",
            "confidence": "high",
            "impact_scope": "전체",
            "evidence_ids": [s04.evidence_id],
        }
    )
    llm = ScriptedLLM(script=[triage, AIMessage(content="조사 완료"), report], calls=[])

    result = run_investigation(
        out, CONFIG, engine, llm_factory=lambda m: llm, k8s=reader, run_id="inv-1"
    )

    assert result.status == "done", result.error
    assert result.report.evidence_ids == [s04.evidence_id]
    assert EvidenceStore(engine).get(result.report.preliminary_facts[0].evidence_ids[0]) is not None


def test_build_request_carries_user_symptom(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    req = build_request(out, CONFIG, trigger="investigate", symptom="채팅이 안 돼요")

    assert (req.trigger, req.user_symptom) == ("investigate", "채팅이 안 돼요")


def test_run_investigation_passes_symptom_to_the_agent(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)
    triage = json.dumps({"issues": [], "runbook": "service-unreachable"})
    report = json.dumps({"conclusion": "c", "confidence": "low", "impact_scope": "전체"})
    llm = ScriptedLLM(script=[triage, AIMessage(content="조사 완료"), report], calls=[])

    run_investigation(
        out,
        CONFIG,
        engine,
        llm_factory=lambda m: llm,
        k8s=_reader("s04/localhost"),
        run_id="inv-2",
        symptom="채팅이 안 돼요",
    )

    assert any("채팅이 안 돼요" in str(m.content) for m in llm.calls[0])
