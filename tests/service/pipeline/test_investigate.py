import json

import pytest
from langchain_core.messages import AIMessage

from core.config import ApiScenario
from core.db import EvidenceStore, create_db_engine
from service.pipeline.check import run_check
from service.pipeline.investigate import build_request, run_investigation
from tests.service.agent.investigator.test_v1_graph import ScriptedLLM
from tests.service.checks.detectors.cases import CONFIG, NOW, FixtureReader, raw_cluster
from tests.service.tools.fakes import FakeHttp

PLAN = '{"hypotheses": [], "order": ["run", "config", "wiring", "logs"]}'


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
            "coverage": {
                a: {"status": "not_applicable", "summary": "테스트"}
                for a in ("run", "config", "wiring", "logs")
            },
            "evidence_ids": [s04.evidence_id],
        }
    )
    llm = ScriptedLLM(script=[triage, PLAN, AIMessage(content="조사 완료"), report], calls=[])

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


def test_build_request_carries_failed_symptoms_from_the_gate(engine):
    config = CONFIG.model_copy(
        update={
            "core_paths": {"chat": ["gateway"]},
            "api_scenarios": [
                ApiScenario.model_validate(
                    {
                        "name": "chat",
                        "feature": "chat",
                        "steps": [{"call": {"via": "gateway", "path": "/chat", "method": "GET"}}],
                    }
                )
            ],
        }
    )
    out = run_check(
        _reader(),
        config,
        engine,
        trigger="check",
        now=NOW,
        http=FakeHttp({("gateway", "/chat"): 500}),
    )

    req = build_request(out, config, trigger="check")

    assert req.gate_path == "B"
    assert [(x.scenario, x.failed_step) for x in req.symptoms] == [("chat", 1)]


def test_build_request_masks_secrets_in_user_symptom(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    req = build_request(
        out, CONFIG, trigger="investigate", symptom="로그인이 안 돼요 password=hunter22abc"
    )

    assert "hunter22abc" not in req.user_symptom
    assert "로그인이 안 돼요" in req.user_symptom


def test_build_request_drops_blank_user_symptom(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    assert build_request(out, CONFIG, trigger="investigate", symptom="  ").user_symptom is None


# ── 조사 대기열 (#28) ──


def test_queue_investigations_puts_request_for_new_incident(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue
    from service.model import InvestigationRequest
    from service.pipeline.investigate import queue_investigations

    out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=NOW)
    queue_investigations(engine, CONFIG, out, trigger="schedule", now=NOW, symptom="채팅 실패")
    job = InvestigationQueue(engine).claim(NOW, timedelta(minutes=10))

    assert (job.incident_id, job.check_run_id) == (out.incidents[0].incident_id, out.run_id)
    request = InvestigationRequest.model_validate(job.request)
    assert (request.trigger, request.gate_path, request.user_symptom) == (
        "schedule",
        "A",
        "채팅 실패",
    )
    assert "S01" in {s.rule_id for ss in request.signal_board.by_service.values() for s in ss}


def test_continuing_incident_is_not_requeued(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue
    from service.pipeline.investigate import queue_investigations

    queue = InvestigationQueue(engine)
    for minutes in (0, 5):
        at = NOW + timedelta(minutes=minutes)
        out = run_check(_reader("s01/detected"), CONFIG, engine, trigger="schedule", now=at)
        queue_investigations(engine, CONFIG, out, trigger="schedule", now=at)
        if minutes == 0:
            job = queue.claim(at, timedelta(minutes=10))
            queue.complete(job, "done", None)

    assert queue.claim(NOW + timedelta(minutes=5), timedelta(minutes=10)) is None
