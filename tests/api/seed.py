"""화면 테스트용 DB 시드. 저장소 API만 써서 실제 점검·조사가 남기는 모양으로 채운다."""

from datetime import UTC, datetime, timedelta
from typing import Any

from core.db import (
    CheckResultStore,
    CheckRunStore,
    EvidenceStore,
    IncidentStore,
    InvestigationStore,
)
from core.db.tables import IncidentCheckRow
from service.agent.base import AgentResult, AgentStep, Usage
from service.model import Evidence, GateResult, Issue, Report, Signal, Symptom

T0 = datetime(2026, 11, 2, 5, 5, 0, tzinfo=UTC)  # 화면에는 14:05 (Asia/Seoul)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_run(engine, trigger: str = "schedule", start: datetime = T0) -> int:
    return CheckRunStore(engine).create(trigger, start)


def fail_chat_symptom(evidence_id: str | None = None) -> Symptom:
    return Symptom(
        scenario="chat",
        status="failed",
        failed_step=2,
        summary="chat 2단계 실패 (HTTP 200 + 오류 문구)",
        severity="critical",
        evidence_id=evidence_id,
    )


def finish_check(
    engine,
    run: int,
    *,
    path: str,
    severity: str | None = None,
    signals: list[Signal] | None = None,
    symptoms: list[Symptom] | None = None,
    reason: str = "",
    rules: tuple[str, ...] = ("S01", "W02", "H01"),
    start: datetime = T0,
    excluded: tuple[str, ...] = (),
) -> None:
    """start → 단계 3개 → finish. 점검이 끝난 모양."""
    store = CheckResultStore(engine)
    signals = (
        signals
        if signals is not None
        else [Signal(rule_id="S01", verdict="passed", subject="dev/Service/a")]
    )
    symptoms = symptoms or []
    store.start(run, start, excluded_rules=excluded)
    store.phase(
        run, "rules", start, start + timedelta(seconds=1), "적발 0 · 관찰 중 0 · 개선 권고 2"
    )
    store.phase(run, "symptoms", at(1), at(4), "chat 2단계 실패" if symptoms else "모두 통과")
    store.phase(run, "gate", at(4), at(5), reason or "조사 안 함")
    gate = GateResult(path=path, severity=severity, reason=reason or "정상")  # type: ignore[arg-type]
    store.finish(run, "done", at(12), gate, signals, symptoms, rules=list(rules), error=None)


def start_running_check(engine, run: int, *, with_phases: int = 1) -> None:
    store = CheckResultStore(engine)
    store.start(run, T0)
    if with_phases:
        store.phase(run, "rules", T0, at(1), "적발 0 · 관찰 중 0 · 개선 권고 2")


def add_evidence(
    engine,
    run: int | None,
    *,
    kind="log_excerpt",
    subject="dev/Pod/agent-x",
    data=None,
    source="GET /logs",
) -> str:
    return EvidenceStore(engine, run).add(
        Evidence(
            kind=kind,  # type: ignore[arg-type]
            subject=subject,
            collected_at=at(30),
            source=source,
            data=data if data is not None else {"line": "429 Too Many Requests"},
        )
    )


def open_incident(engine, run: int, severity: str = "critical", features=("chat",)) -> int:
    return IncidentStore(engine).open(
        "fp-1",
        {"path": "B", "features": list(features), "scenarios": ["chat"], "targets": []},
        severity,
        run,
        T0,
    )


def link_incident(engine, incident_id: int, run: int, action: str) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as s, s.begin():
        s.add(IncidentCheckRow(incident_id=incident_id, check_run_id=run, action=action))


def step(
    node: str, sec: float, *, tool=None, args=None, evidence_ids=None, detail=None
) -> AgentStep:
    return AgentStep(
        node=node,
        started_at=at(sec),
        ended_at=at(sec + 2),
        tool=tool,
        args=args or {},
        evidence_ids=evidence_ids or [],
        detail=detail or {},
    )


def start_investigation(
    engine,
    run: int,
    incident: int | None,
    steps: list[AgentStep],
    *,
    gate_path="B",
    gate_reason="기능 점검 실패",
    trace_id=None,
) -> int:
    store = InvestigationStore(engine)
    iid = store.start(
        check_run_id=run,
        incident_id=incident,
        run_key=f"kg-worker-{run}",
        agent="investigator:v1",
        trigger="schedule",
        now=at(10),
        trace_id=trace_id,
        gate_path=gate_path,
        gate_reason=gate_reason,
    )
    sink = store.steps_sink(iid)
    for s in steps:
        sink.record(s)
    return iid


def finish_investigation(
    engine, iid: int, report: Report | None, status="done", error=None
) -> None:
    InvestigationStore(engine).finish(
        iid,
        AgentResult(
            status=status,
            report=report,
            usage=Usage(tool_calls=9, seconds=48, input_tokens=21000),
            error=error,
        ),  # type: ignore[arg-type]
        at(60),
    )


def report(judgement="confirmed", evidence_ids=("ev-0001",), **kw: Any) -> Report:
    return Report(
        conclusion=kw.pop("conclusion", "llm-gateway 할당량 소진이 원인"),
        confidence="high",
        judgement=judgement,
        judgement_basis="원인 리소스 설정 인용 · 근거 출처 3종(설정·상태, 로그, 앱 API)",
        impact_scope="채팅 전체",
        issues=[
            Issue(
                rank=1,
                title="채팅 실패",
                impact="core_path_broken",
                confidence="high",
                evidence_ids=list(evidence_ids),
            )
        ],
        evidence_ids=list(evidence_ids),
        **kw,
    )
