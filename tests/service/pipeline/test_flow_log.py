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
    assert _lines(caplog) == ["[조사 #12] 계획: 가설 2개 — env가 localhost / 포트 불일치"]


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
        "[조사 #12] 선별: 문제 1개 — 로그인 500",
        "[조사 #12] 사전 확인: 근거 2건",
        "[조사 #12] 재조사: 빈틈 1개",
        "[조사 #12] 상한 도달 (time)",
    ]


def test_free_text_is_redacted_and_truncated(caplog):
    sink = FlowLogSteps("#1")
    secret = "postgres://u:hunter2pass@db/x"
    sink.record(_step("plan", detail={"hypotheses": [f"접속 {secret} " + "가" * 200]}))
    [line] = _lines(caplog)
    assert "hunter2pass" not in line
    assert len(line) < 200


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
