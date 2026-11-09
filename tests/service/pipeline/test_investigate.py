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

    req = build_request(out, CONFIG, trigger="check", ai_on_rules=True)

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
        out, CONFIG, engine, llm_factory=lambda m: llm, k8s=reader, run_id="inv-1", ai_on_rules=True
    )

    assert result.status == "done", result.error
    assert result.report.evidence_ids == [s04.evidence_id]
    assert EvidenceStore(engine).get(result.report.preliminary_facts[0].evidence_ids[0]) is not None


def test_build_request_carries_user_symptom(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    req = build_request(
        out, CONFIG, trigger="investigate", symptom="채팅이 안 돼요", ai_on_rules=True
    )

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
        ai_on_rules=True,
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
        out,
        CONFIG,
        trigger="investigate",
        symptom="로그인이 안 돼요 password=hunter22abc",
        ai_on_rules=True,
    )

    assert "hunter22abc" not in req.user_symptom
    assert "로그인이 안 돼요" in req.user_symptom


def test_build_request_drops_blank_user_symptom(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    req = build_request(out, CONFIG, trigger="investigate", symptom="  ", ai_on_rules=True)

    assert req.user_symptom is None


# ── 조사 대기열 (#28) ──

SCENARIO_CONFIG = CONFIG.model_copy(
    update={
        "core_paths": {"chat": ["gateway", "agent"]},
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


def _scheduled(engine, case=None, *, status=500, minutes=0):
    """주기 점검 한 번 (기능 시나리오 chat이 status를 받는다) + 조사 예약."""
    from datetime import timedelta

    from service.pipeline.investigate import queue_investigations

    at = NOW + timedelta(minutes=minutes)
    out = run_check(
        _reader(case),
        SCENARIO_CONFIG,
        engine,
        trigger="schedule",
        now=at,
        http=FakeHttp({("gateway", "/chat"): status}),
    )
    queue_investigations(engine, SCENARIO_CONFIG, out, trigger="schedule", now=at)
    return out


def _incident(engine, out):
    from core.db import IncidentStore

    return IncidentStore(engine).get(out.incidents[0].incident_id)


def test_queue_investigations_puts_request_for_new_incident(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue
    from service.model import InvestigationRequest
    from service.pipeline.investigate import queue_investigations

    out = run_check(
        _reader(),
        SCENARIO_CONFIG,
        engine,
        trigger="schedule",
        now=NOW,
        http=FakeHttp({("gateway", "/chat"): 500}),
    )
    queue_investigations(
        engine, SCENARIO_CONFIG, out, trigger="schedule", now=NOW, symptom="채팅 실패"
    )
    job = InvestigationQueue(engine).claim(NOW, timedelta(minutes=10))

    assert (job.incident_id, job.check_run_id) == (out.incidents[0].incident_id, out.run_id)
    request = InvestigationRequest.model_validate(job.request)
    assert (request.trigger, request.gate_path, request.user_symptom) == (
        "schedule",
        "B",
        "채팅 실패",
    )
    assert [x.scenario for x in request.symptoms] == ["chat"]


def test_continuing_incident_is_not_requeued(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue

    queue = InvestigationQueue(engine)
    for minutes in (0, 5):
        out = _scheduled(engine, minutes=minutes)
        if minutes == 0:
            job = queue.claim(NOW, timedelta(minutes=10))
            queue.complete(job, "done", None)

    assert out.incidents[0].action == "continued"
    assert queue.claim(NOW + timedelta(minutes=5), timedelta(minutes=10)) is None


# ── 규칙 감지는 AI 조사 없이 (#185) ──


def test_gate_a_builds_no_request_unless_evaluation_asks(engine):
    out = run_check(_reader("s04/localhost"), CONFIG, engine, trigger="check", now=NOW)

    assert out.gate.path == "A"
    assert build_request(out, CONFIG, trigger="check") is None
    assert build_request(out, CONFIG, trigger="check", ai_on_rules=True) is not None


def test_run_investigation_does_not_call_llm_on_rule_detection(engine):
    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)

    def no_llm(model):
        raise AssertionError("규칙 감지는 LLM을 부르지 않는다")

    result = run_investigation(out, CONFIG, engine, run_id="r", llm_factory=no_llm, k8s=reader)

    assert result is None


def test_rule_detection_queues_nothing_and_marks_incident_rule(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue, InvestigationStore

    out = _scheduled(engine, "s01/detected", status=200)

    assert out.gate.path == "A" and out.incidents[0].action == "opened"
    assert InvestigationQueue(engine).claim(NOW, timedelta(minutes=10)) is None
    assert _incident(engine, out).investigation == "rule"
    assert InvestigationStore(engine).for_check(out.run_id) == []  # AI 조사 이력을 만들지 않는다


def test_rule_plus_functional_failure_stays_on_rule_path(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue

    out = _scheduled(engine, "s01/detected", status=500)

    assert out.gate.path == "A" and [x.scenario for x in out.gate.symptoms] == ["chat"]
    assert InvestigationQueue(engine).claim(NOW, timedelta(minutes=10)) is None
    assert _incident(engine, out).investigation == "rule"


def test_rule_to_symptom_change_queues_ai_investigation(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue

    first = _scheduled(engine, "s01/detected", status=500)
    second = _scheduled(engine, None, status=500, minutes=5)  # 규칙 신호가 사라지고 기능 실패만

    assert (second.gate.path, second.incidents[0].action) == ("B", "changed")
    assert second.incidents[0].incident_id == first.incidents[0].incident_id
    job = InvestigationQueue(engine).claim(NOW, timedelta(minutes=10))
    assert job is not None and job.request["gate_path"] == "B"
    assert _incident(engine, second).investigation == "running"


def test_symptom_to_rule_change_removes_pending_request(engine):
    from datetime import timedelta

    from core.db import InvestigationQueue

    first = _scheduled(engine, None, status=500)
    assert _incident(engine, first).investigation == "pending"
    second = _scheduled(engine, "s01/detected", status=500, minutes=5)

    assert (second.gate.path, second.incidents[0].action) == ("A", "changed")
    assert InvestigationQueue(engine).claim(NOW, timedelta(hours=1)) is None
    assert _incident(engine, second).investigation == "rule"


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
        ai_on_rules=True,
    )

    # 예전 스크립트 보고서도 읽지만 호출 체인 없는 원인 주장을 새로 확정하지 않는다.
    assert result.report.judgement == "candidate"
    assert result.report.judgement_basis == "호출 체인이 없습니다"


def test_run_investigation_confirms_with_cause_config_and_supported_hypothesis(engine):
    reader = _reader("s04/localhost")
    out = run_check(reader, CONFIG, engine, trigger="check", now=NOW)
    triage = json.dumps(
        {
            "issues": [
                {
                    "rank": 1,
                    "title": "gateway 주소 설정 오류",
                    "impact": "core_path_broken",
                    "confidence": "high",
                }
            ],
            "runbook": "service-unreachable",
        }
    )

    def call(name, args, call_id):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])

    def report():
        # 도구가 저장한 근거(원인 리소스 설정, 이벤트)를 번호로 인용한다
        stored = EvidenceStore(engine).list_for_run(out.run_id, limit=50)
        config_id = next(e.id for e in stored if e.subject.endswith("/Deployment/gateway"))
        event_id = next(e.id for e in stored if e.kind == "event")
        return json.dumps(
            {
                "conclusion": "gateway 주소 env가 localhost",
                "confidence": "high",
                "impact_scope": "전체",
                "cause_chain": [
                    {
                        "service": {"kind": "Deployment", "name": "gateway"},
                        "observation": "주소 설정 오류",
                        "evidence_ids": [config_id],
                        "error_from_downstream": False,
                    }
                ],
                "coverage": {
                    a: {"status": "not_applicable", "summary": "테스트"}
                    for a in ("run", "config", "wiring", "logs")
                },
                "issues": [
                    {
                        "rank": 1,
                        "title": "gateway 주소 설정 오류",
                        "impact": "core_path_broken",
                        "confidence": "high",
                        "cause_resource": {"kind": "Deployment", "name": "gateway"},
                        "evidence_ids": [config_id],
                    }
                ],
                "hypotheses": [
                    {
                        "statement": "gateway 주소 오류",
                        "status": "supported",
                        "reason": "설정과 이벤트가 같은 원인을 가리킴",
                        "evidence_ids": [config_id, event_id],
                    }
                ],
                "evidence_ids": [config_id, event_id],
            }
        )

    script = [
        triage,
        PLAN,
        call("get_resource", {"kind": "Deployment", "name": "gateway"}, "c1"),
        call("get_events", {}, "c2"),
        AIMessage(content="조사 완료"),
        report,
    ]
    llm = ScriptedLLM(script=script, calls=[])

    result = run_investigation(
        out,
        CONFIG,
        engine,
        run_id="t-c",
        llm_factory=lambda m: llm,
        k8s=reader,
        http=FakeHttp(),
        ai_on_rules=True,
    )

    assert result.status == "done", result.error
    assert result.report.judgement == "confirmed"
    assert result.report.judgement_basis == (
        "원인 리소스 설정 인용 · 근거 출처 2종(설정·상태, 이벤트) · 지지 가설 근거 2종"
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
        ai_on_rules=True,
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
        ai_on_rules=True,
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


def test_rule_incident_recovery_and_continuation_flow_is_unchanged(engine):
    from datetime import timedelta

    from core.db import AlertStore, IncidentStore, InvestigationQueue
    from service.notify.notifier import Notifier
    from service.notify.sender import DryRunSender

    def check(case, minutes):
        out = _scheduled(engine, case, status=200, minutes=minutes)
        at = NOW + timedelta(minutes=minutes)
        Notifier(engine, SCENARIO_CONFIG, DryRunSender()).after_check(
            out.run_id, out.gate, out.incidents, at
        )
        return out

    opened = check("s01/detected", 0)
    continued = check("s01/detected", 5)
    resolved = check(None, 10)

    assert [c.action for c in (opened.incidents[0], continued.incidents[0])] == [
        "opened",
        "continued",
    ]
    incident_id = opened.incidents[0].incident_id
    assert resolved.incidents[0].action == "resolved"
    assert IncidentStore(engine).get(incident_id).status == "resolved"
    assert [a.kind for a in AlertStore(engine).for_incident(incident_id)] == ["M1", "M3"]
    assert InvestigationQueue(engine).claim(NOW, timedelta(hours=1)) is None


def test_login_step_failure_maps_to_the_login_path_only_when_core_paths_has_one():
    from core.config import KubeGuardianConfig
    from service.pipeline.investigate import _login_step_services

    scenario = {
        "name": "chat",
        "feature": "chat",
        "steps": [
            {"login": {"via": "gateway", "path": "/l", "account_secret": "s", "token": "t"}},
            {"call": {"via": "gateway", "path": "/c", "expect": 200}},
        ],
    }
    base = {"target": {"namespace": "dev"}, "api_scenarios": [scenario]}

    with_login = KubeGuardianConfig.model_validate(
        {**base, "core_paths": {"login": ["gateway", "admin"], "chat": ["gateway", "agent"]}}
    )
    without_login = KubeGuardianConfig.model_validate({**base, "core_paths": {"chat": ["gateway"]}})

    assert _login_step_services(with_login) == {"chat": {1: ["gateway", "admin"]}}
    assert _login_step_services(without_login) == {}
