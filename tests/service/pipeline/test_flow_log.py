import logging
from datetime import UTC, datetime

import pytest

from core.db import InvestigationStore, create_db_engine
from service.agent.base import AgentResult, AgentStep, Usage
from service.model import Report
from service.model.report import Hypothesis
from service.pipeline.flow_log import FlowLogSteps, log_end, log_start

NOW = datetime(2026, 11, 9, tzinfo=UTC)


def _step(node, **kw):
    return AgentStep(node=node, started_at=NOW, ended_at=NOW, **kw)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "kubeguardian"]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.INFO, logger="kubeguardian")


def test_plan_step_logs_hypothesis_count_and_titles(caplog):
    sink = FlowLogSteps("#12")
    sink.record(
        _step("plan", detail={"issue": "x", "hypotheses": ["env가 localhost", "포트 불일치"]})
    )
    assert _lines(caplog) == [
        "[조사 #12] 계획: 가설 2개",
        "[조사 #12]   - 가설 1: env가 localhost",
        "[조사 #12]   - 가설 2: 포트 불일치",
    ]


def test_tool_step_logs_tool_target_and_evidence(caplog):
    sink = FlowLogSteps("#12")
    sink.record(
        _step(
            "tools",
            tool="get_resource",
            args={"kind": "Deployment", "name": "admin"},
            evidence_ids=["ev-0031"],
        )
    )
    assert _lines(caplog) == ["[조사 #12] 도구 get_resource Deployment/admin → 근거 ev-0031"]


def test_triage_and_other_notable_steps(caplog):
    sink = FlowLogSteps("#12")
    sink.record(_step("triage", detail={"issues": ["로그인 500"], "runbook": "r"}))
    sink.record(_step("preliminary", evidence_ids=["ev-0001", "ev-0002"]))
    sink.record(_step("stop_check", detail={"gaps": ["g"], "recheck": True}))
    sink.record(_step("limit", detail={"stop_reason": "time"}))
    sink.record(_step("act", detail={"tool_calls": ["get_events"]}))  # 잡음은 남기지 않는다
    assert _lines(caplog) == [
        "[조사 #12] 선별: 문제 1개 — 로그인 500 · 조사 지침: r",
        "[조사 #12] 사전 확인: 근거 2건",
        "[조사 #12] 재조사: 빈틈 1개 — 한 번 더 조사합니다",
        "[조사 #12]   - g",
        "[조사 #12] 상한 도달 (time)",
    ]


def test_free_text_is_redacted_and_truncated(caplog):
    sink = FlowLogSteps("#1")
    secret = "postgres://u:hunter2pass@db/x"
    sink.record(_step("plan", detail={"hypotheses": [f"접속 {secret} " + "가" * 200]}))
    header, line = _lines(caplog)
    assert "hunter2pass" not in line
    assert len(line) < 200


def test_detail_lines_are_redacted_and_clipped_to_120(caplog):
    secret = "postgres://u:hunter2pass@db/x"
    sink = FlowLogSteps("#1")
    sink.record(
        _step(
            "preliminary",
            evidence_ids=["ev-1"],
            detail={"facts": [{"statement": f"{secret} " + "가" * 300, "evidence_ids": ["ev-1"]}]},
        )
    )
    sink.record(_step("stop_check", detail={"gaps": [f"{secret} " + "나" * 300], "recheck": True}))
    text = "\n".join(_lines(caplog))
    assert "hunter2pass" not in text
    assert all(len(line) < 160 for line in _lines(caplog))


def test_knowledge_step_logs_injected_knowledge(caplog):
    FlowLogSteps("#12").record(
        _step(
            "knowledge",
            detail={
                "environment": 1,
                "runbooks": ["auth-failure", "partial-failure"],
                "chars": 12345,
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 지식 주입: 환경 설명 1건 · 조사 지침 2종(auth-failure, partial-failure) "
        "· 약 12,345자 (전체 주입)"
    ]


def test_triage_logs_chosen_runbook_with_title(caplog):
    FlowLogSteps("#12").record(
        _step(
            "triage",
            detail={
                "issues": ["로그인 500"],
                "runbook": "partial-failure",
                "runbook_title": "요청의 일부만 실패",
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 선별: 문제 1개 — 로그인 500 · 조사 지침: partial-failure(요청의 일부만 실패)"
    ]


def test_preliminary_logs_each_fact_with_evidence_numbers(caplog):
    FlowLogSteps("#12").record(
        _step(
            "preliminary",
            evidence_ids=["ev-1", "ev-2"],
            detail={
                "facts": [
                    {"statement": "get_topology: 서비스 5개", "evidence_ids": ["ev-1"]},
                    {"statement": "호출 구간 표", "evidence_ids": ["ev-1", "ev-2"]},
                ]
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 사전 확인: 근거 2건",
        "[조사 #12]   - get_topology: 서비스 5개 [ev-1]",
        "[조사 #12]   - 호출 구간 표 [ev-1, ev-2]",
    ]


def test_plan_logs_runbook_and_area(caplog):
    FlowLogSteps("#12").record(
        _step(
            "plan",
            detail={
                "hypotheses": ["env가 localhost"],
                "areas": ["config"],
                "runbook": "service-unreachable",
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 계획: 가설 1개 · 지침 service-unreachable",
        "[조사 #12]   - 가설 1 (설정): env가 localhost",
    ]


def test_tool_call_during_recheck_is_marked(caplog):
    FlowLogSteps("#12").record(
        _step(
            "tools",
            tool="get_resource",
            args={"kind": "Deployment", "name": "admin"},
            evidence_ids=["ev-0040"],
            detail={"recheck": True},
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] (재조사) 도구 get_resource Deployment/admin → 근거 ev-0040"
    ]


def test_validate_logs_counts_and_demoted_items(caplog):
    FlowLogSteps("#12").record(
        _step(
            "validate",
            detail={
                "cited": 5,
                "confirmed": 4,
                "removed": 1,
                "demoted": ["가설 A"],
                "conclusion_supported": True,
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 근거 검증: 보고서 인용 5건 중 4건 확인 · 제거 1건 · 강등 1건 · 결론 근거 있음",
        "[조사 #12]   - 강등: 가설 A",
    ]


def test_validate_without_counts_logs_nothing(caplog):
    FlowLogSteps("#12").record(_step("validate", detail={"demoted": []}))
    assert _lines(caplog) == []


def test_validate_says_when_conclusion_has_no_evidence(caplog):
    FlowLogSteps("#12").record(
        _step(
            "validate",
            detail={
                "cited": 0,
                "confirmed": 0,
                "removed": 0,
                "demoted": [],
                "conclusion_supported": False,
            },
        )
    )
    assert _lines(caplog) == [
        "[조사 #12] 근거 검증: 보고서 인용 0건 중 0건 확인 · 제거 0건 · 강등 0건 · 결론 근거 없음"
    ]


def test_stop_check_logs_remaining_gaps_or_none(caplog):
    sink = FlowLogSteps("#12")
    sink.record(
        _step("stop_check", detail={"gaps": ["a", "b"], "recheck": False, "rechecked": True})
    )
    sink.record(_step("stop_check", detail={"gaps": ["c"], "recheck": False, "rechecked": False}))
    sink.record(_step("stop_check", detail={"gaps": [], "recheck": False}))
    assert _lines(caplog) == [
        "[조사 #12] 빈틈 검사: 2개 남음(재조사 상한)",
        "[조사 #12]   - a",
        "[조사 #12]   - b",
        "[조사 #12] 빈틈 검사: 1개 남음(시간·호출 상한)",
        "[조사 #12]   - c",
        "[조사 #12] 빈틈 검사: 없음",
    ]


def test_write_report_logs_attempt_and_reason_for_rewrite(caplog):
    sink = FlowLogSteps("#12")
    sink.record(_step("write_report", detail={"attempt": 1}))
    sink.record(_step("write_report", detail={"attempt": 2}))
    assert _lines(caplog) == [
        "[조사 #12] 보고서 작성(시도 1)",
        "[조사 #12] 보고서 작성(시도 2) — 결론에 근거 번호가 없어 다시 씀",
    ]


def test_steps_still_reach_inner_sink(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    store = InvestigationStore(engine)
    inner = store.steps_sink(0)
    sink = FlowLogSteps("#1", inner)
    sink.record(_step("plan", detail={"hypotheses": []}))
    assert [s.node for s in sink.steps] == ["plan"]
    assert [s.node for s in inner.steps] == ["plan"]


def test_logging_failure_never_breaks_recording():
    sink = FlowLogSteps("#1")
    sink.record(_step("plan", detail={"hypotheses": 5}))  # 형식이 틀려도 기록은 남는다
    assert len(sink.steps) == 1


def test_start_line(caplog):
    class Req:
        gate_path = "A"

    log_start("#12", Req(), incident_id=5, reason="S04 적발")
    assert _lines(caplog) == ["[조사 #12] 시작: 이슈 #5 경로 A — S04 적발"]


def test_end_lines_with_verdicts_and_summary(caplog):
    report = Report(
        conclusion="gateway 주소 env가 localhost라 연결이 거부됨",
        confidence="high",
        impact_scope="전체",
        judgement="confirmed",
        judgement_basis="원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태)",
        hypotheses=[
            Hypothesis(statement="env 오설정", status="supported"),
            Hypothesis(statement="포트 불일치", status="refuted"),
        ],
    )
    result = AgentResult(status="done", report=report, usage=Usage(tool_calls=12, seconds=165.4))
    log_end("#12", result)
    assert _lines(caplog) == [
        "[조사 #12] 가설 판정: env 오설정 지지 / 포트 불일치 반증",
        "[조사 #12] 결론: gateway 주소 env가 localhost라 연결이 거부됨 "
        "(판단 수준 확정, 165s, 도구 12회)",
        "[조사 #12] 판단 근거: 원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태)",
    ]


def test_end_line_for_failure(caplog):
    log_end("#3", AgentResult(status="failed", error="TimeoutError", usage=Usage(seconds=3)))
    assert _lines(caplog) == ["[조사 #3] 실패: TimeoutError (3s)"]


def test_log_functions_never_raise(monkeypatch):
    from service.pipeline import flow_log

    class Boom:
        def info(self, *a, **k):
            raise RuntimeError("logger down")

        def debug(self, *a, **k):
            pass

    monkeypatch.setattr(flow_log, "logger", Boom())

    class Req:
        gate_path = "A"

    log_start("#1", Req(), incident_id=1, reason="x")
    log_end("#1", AgentResult(status="failed", error="e"))


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        (
            "get_resource",
            {"kind": "ReplicaSet", "name": None, "label_selector": "app=gateway"},
            "도구 get_resource ReplicaSet app=gateway",
        ),
        (
            "get_resource",
            {"kind": "Pod", "name": None, "label_selector": None},
            "도구 get_resource Pod",
        ),
        (
            "get_pod_logs",
            {"pod": "gateway-5cb", "container": "gateway", "previous": False, "tail": 60},
            "도구 get_pod_logs gateway-5cb gateway 60",
        ),
    ],
)
def test_tool_target_skips_none_and_booleans(caplog, tool, args, expected):
    FlowLogSteps("#12").record(_step("tools", tool=tool, args=args))
    assert _lines(caplog) == [f"[조사 #12] {expected}"]
