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


# ── 판단 수준 (#106) ──


def test_run_investigation_attaches_judgement_from_stored_evidence(engine):
    from tests.test_cli import _scripted

    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)
    llm = _scripted()  # 역할마다 같은 스크립트를 이어 쓴다

    result = run_investigation(
        out,
        CONFIG,
        engine,
        run_id="t",
        llm_factory=lambda m: llm,
        k8s=reader,
        http=FakeHttp(),
    )

    # 스크립트 보고서는 신호판 S04 근거(설정·상태)만 인용하고 원인 리소스 설정은 인용하지 않는다
    assert result.report.judgement == "candidate"
    assert (
        result.report.judgement_basis
        == "원인 리소스 설정 미인용 · 근거 출처 1종(설정·상태) · 지지된 가설 없음"
    )


# ── 조사 기록 (#98) ──


def test_investigation_and_steps_are_stored_for_the_check(engine):
    from core.db import InvestigationStore
    from tests.test_cli import _scripted

    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)
    llm = _scripted()

    result = run_investigation(
        out,
        CONFIG,
        engine,
        run_id="t-1",
        llm_factory=lambda m: llm,
        k8s=reader,
        http=FakeHttp(),
    )

    store = InvestigationStore(engine)
    [inv] = store.for_check(out.run_id)
    assert (inv.status, inv.run_key, inv.trigger) == ("done", "t-1", "check")
    assert inv.report == result.report and inv.ended_at is not None
    assert inv.report.judgement == "candidate"  # 판단 수준까지 붙은 보고서가 저장된다
    steps = store.steps(inv.id)
    assert [s.node for s in steps] == [s.node for s in result.steps]  # 결과에도 그대로 남는다
    assert steps[0].node == "preliminary"


# ── 리뷰 지적: 실패 경로·저장 실패·추적 (#98 / PR #112) ──


def _run(engine, **kw):
    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)
    from tests.test_cli import _scripted

    llm = _scripted()
    return out, run_investigation(
        out,
        CONFIG,
        engine,
        run_id="t-x",
        llm_factory=lambda m: llm,
        k8s=reader,
        http=FakeHttp(),
        **kw,
    )


def test_exception_after_start_marks_row_failed_and_reraises(engine, monkeypatch):
    from core.db import InvestigationStore
    from service.pipeline import investigate

    def boom(*a, **k):
        raise RuntimeError("agent broke")

    monkeypatch.setattr(investigate, "_run_agent", boom)

    with pytest.raises(RuntimeError):
        _run(engine)

    [inv] = InvestigationStore(engine).for_check(1)
    assert (inv.status, inv.error, inv.ended_at is not None) == ("failed", "RuntimeError", True)


def test_base_exception_marks_row_failed_and_reraises(engine, monkeypatch):
    from core.db import InvestigationStore
    from service.pipeline import investigate

    def term(*a, **k):
        raise SystemExit(143)

    monkeypatch.setattr(investigate, "_run_agent", term)

    with pytest.raises(SystemExit):
        _run(engine)

    [inv] = InvestigationStore(engine).for_check(1)
    assert (inv.status, inv.error) == ("failed", "INTERRUPTED")  # sweep_stale과 같은 값


def test_keyboard_interrupt_marks_row_interrupted_and_reraises(engine, monkeypatch):
    from core.db import InvestigationStore
    from service.pipeline import investigate

    def ctrl_c(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(investigate, "_run_agent", ctrl_c)

    with pytest.raises(KeyboardInterrupt):
        _run(engine)

    [inv] = InvestigationStore(engine).for_check(1)
    assert (inv.status, inv.error) == ("failed", "INTERRUPTED")


def test_judge_failure_keeps_the_finished_report_without_judgement(engine, monkeypatch, caplog):
    from core.db import InvestigationStore
    from service.pipeline import investigate

    def boom(*a, **k):
        raise RuntimeError("secret-ish detail")

    monkeypatch.setattr(investigate, "judge", boom)

    out, result = _run(engine)

    assert result.status == "done" and result.report is not None
    assert result.report.judgement is None
    [inv] = InvestigationStore(engine).for_check(out.run_id)
    assert inv.status == "done" and inv.report.judgement is None
    assert "secret-ish detail" not in caplog.text


def test_finish_storage_error_still_returns_the_result(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from core.db import InvestigationStore

    def locked(*a, **k):
        raise OperationalError("update", {}, Exception("database is locked"))

    monkeypatch.setattr(InvestigationStore, "finish", locked)

    _, result = _run(engine)

    assert result.status == "done" and result.report is not None


def test_start_storage_error_runs_without_a_row(engine, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from core.db import InvestigationStore

    def locked(*a, **k):
        raise OperationalError("insert", {}, Exception("database is locked"))

    monkeypatch.setattr(InvestigationStore, "start", locked)

    out, result = _run(engine)

    assert result.status == "done" and result.steps
    assert InvestigationStore(engine).for_check(out.run_id) == []


def test_gate_and_symptom_are_stored_and_trace_is_none_without_tracing(engine):
    from core.db import InvestigationStore

    out, _ = _run(engine, symptom="채팅이 안 돼요")

    [inv] = InvestigationStore(engine).for_check(out.run_id)
    assert (inv.gate_path, inv.gate_reason, inv.user_symptom) == (
        "A",
        out.gate.reason,
        "채팅이 안 돼요",
    )
    assert inv.trace_id is None


def test_trace_id_of_the_investigation_span_is_stored(engine, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    from core.db import InvestigationStore
    from service.pipeline import investigate

    provider = TracerProvider()
    monkeypatch.setattr(investigate.trace, "get_tracer", provider.get_tracer)

    out, _ = _run(engine)

    [inv] = InvestigationStore(engine).for_check(out.run_id)
    assert inv.trace_id is not None and len(inv.trace_id) == 32
