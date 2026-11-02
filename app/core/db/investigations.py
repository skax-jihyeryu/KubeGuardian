"""조사 기록: 조사 한 번과 그 단계·보고서 (보고서 화면 §6.4 조사, #98).

- 단계는 끝날 때마다 바로 쓴다(SqliteSteps). 진행 중인 조사의 단계를 화면이 수 초마다 다시 읽는다.
- 끝나면 결과(상태·보고서·사용량·오류)를 같은 행에 남긴다. 단계 값은 에이전트가 이미 마스킹했고,
  보고서는 저장 직전에 한 번 더 마스킹한다.
- 기록은 부가 작업이다. 단계를 못 써도 조사는 이어진다(메모리에 남는다).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.db.tables import AgentStepRow, InvestigationRow, require_aware
from core.guard.redact import redact_mapping
from core.log.logging import get_logging
from service.agent.base import AgentResult, AgentStep, MemorySteps, Usage
from service.model import Report

logger = get_logging()

INTERRUPTED = "INTERRUPTED"  # 끝을 기록하지 못하고 끊긴 조사(SIGKILL·OOM·Pod 교체)


@dataclass(frozen=True)
class Investigation:
    id: int
    check_run_id: int
    incident_id: int | None
    run_key: str
    agent: str
    trigger: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    report: Report | None
    usage: Usage | None
    error: str | None
    report_invalid: bool = False  # 저장된 보고서가 현재 모델로 읽히지 않음 (report는 None)
    trace_id: str | None = None  # Phoenix 추적 (추적을 켰을 때만)
    gate_path: str | None = None
    gate_reason: str | None = None
    user_symptom: str | None = None  # 마스킹된 값


class SqliteSteps(MemorySteps):
    """StepSink: 단계를 DB에 바로 쓰고, 결과(AgentResult.steps)용으로 메모리에도 남긴다."""

    def __init__(self, engine: Engine, investigation_id: int):
        super().__init__()
        self._engine = engine
        self._investigation_id = investigation_id

    def record(self, step: AgentStep) -> None:
        super().record(step)
        row = AgentStepRow(
            investigation_id=self._investigation_id,
            node=step.node,
            started_at=require_aware(step.started_at),
            ended_at=step.ended_at and require_aware(step.ended_at),
            tool=step.tool,
            args=step.args,
            evidence_ids=step.evidence_ids,
            detail=step.detail,
        )
        try:
            with Session(self._engine) as s, s.begin():
                s.add(row)
        except SQLAlchemyError as e:  # 기록 실패가 조사를 멈추지 않게 (메모리 사본은 이미 남았다)
            logger.warning(f"조사 단계 기록 실패: {type(e).__name__}")


class InvestigationStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def start(
        self,
        *,
        check_run_id: int,
        incident_id: int | None,
        run_key: str,
        agent: str,
        trigger: str,
        now: datetime,
        trace_id: str | None = None,
        gate_path: str | None = None,
        gate_reason: str | None = None,
        user_symptom: str | None = None,
    ) -> int:
        """조사를 시작한다. 같은 run_key로 끝나지 않은 행(죽은 조사)은 같은 트랜잭션에서 닫는다.

        run_key는 UNIQUE가 아니다 (재시도는 정상이라 행이 여럿일 수 있다).
        """
        row = InvestigationRow(
            check_run_id=check_run_id,
            incident_id=incident_id,
            run_key=run_key,
            agent=agent,
            trigger=trigger,
            status="running",
            started_at=require_aware(now),
            trace_id=trace_id,
            gate_path=gate_path,
            gate_reason=gate_reason,
            user_symptom=user_symptom,
        )
        with Session(self._engine) as s, s.begin():
            s.execute(
                update(InvestigationRow)
                .where(InvestigationRow.run_key == run_key, InvestigationRow.status == "running")
                .values(status="failed", error=INTERRUPTED, ended_at=require_aware(now))
            )
            s.add(row)
            s.flush()
            return row.id

    def sweep_stale(self, now: datetime, stale: timedelta) -> int:
        """시작한 지 stale이 지나도 running인 조사를 INTERRUPTED로 닫는다. 닫은 행 수.

        SIGKILL·OOM은 잡을 수 없어 끝을 못 쓴다. 작업자가 매 주기 부른다 (kg investigate 행 포함).
        """
        now = require_aware(now)
        cutoff = (now - stale).isoformat()
        stmt = (
            update(InvestigationRow)
            .where(
                InvestigationRow.status == "running",
                func.julianday(InvestigationRow.started_at) <= func.julianday(cutoff),
            )
            .values(status="failed", error=INTERRUPTED, ended_at=now)
        )
        with Session(self._engine) as s, s.begin():
            return s.execute(stmt).rowcount

    def steps_sink(self, investigation_id: int) -> SqliteSteps:
        return SqliteSteps(self._engine, investigation_id)

    def finish(self, investigation_id: int, result: AgentResult, now: datetime) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(InvestigationRow, investigation_id)
            row.status = result.status
            row.ended_at = require_aware(now)
            row.report = (
                redact_mapping(result.report.model_dump(mode="json")) if result.report else None
            )
            row.usage = result.usage.model_dump(mode="json")
            row.error = result.error

    def get(self, investigation_id: int) -> Investigation | None:
        with Session(self._engine) as s:
            row = s.get(InvestigationRow, investigation_id)
            return _to_investigation(row) if row else None

    def latest_status_by_check(self, check_run_ids: list[int]) -> dict[int, str]:
        """점검마다 가장 최근 조사의 상태 (목록 화면용, 쿼리 한 번). 조사 없는 점검은 빠진다."""
        if not check_run_ids:
            return {}
        latest = (
            select(func.max(InvestigationRow.id))
            .where(InvestigationRow.check_run_id.in_(check_run_ids))
            .group_by(InvestigationRow.check_run_id)
        )
        stmt = select(InvestigationRow.check_run_id, InvestigationRow.status).where(
            InvestigationRow.id.in_(latest)
        )
        with Session(self._engine) as s:
            return {run_id: status for run_id, status in s.execute(stmt)}

    def for_check(self, check_run_id: int) -> list[Investigation]:
        return self._list(InvestigationRow.check_run_id == check_run_id)

    def for_incident(self, incident_id: int) -> list[Investigation]:
        return self._list(InvestigationRow.incident_id == incident_id)

    def steps(self, investigation_id: int) -> list[AgentStep]:
        stmt = (
            select(AgentStepRow)
            .where(AgentStepRow.investigation_id == investigation_id)
            .order_by(AgentStepRow.id)
        )
        with Session(self._engine) as s:
            return [
                AgentStep(
                    node=r.node,
                    started_at=r.started_at,
                    ended_at=r.ended_at,
                    tool=r.tool,
                    args=r.args or {},
                    evidence_ids=r.evidence_ids or [],
                    detail=r.detail or {},
                )
                for r in s.scalars(stmt)
            ]

    def _list(self, where) -> list[Investigation]:
        stmt = select(InvestigationRow).where(where).order_by(InvestigationRow.id)
        with Session(self._engine) as s:
            return [_to_investigation(r) for r in s.scalars(stmt)]


def _to_investigation(row: InvestigationRow) -> Investigation:
    report, invalid = None, False
    if row.report:
        try:
            report = Report.model_validate(row.report)
        except ValidationError:  # 행 하나가 목록 전체를 깨지 않게
            invalid = True
    return Investigation(
        id=row.id,
        check_run_id=row.check_run_id,
        incident_id=row.incident_id,
        run_key=row.run_key,
        agent=row.agent,
        trigger=row.trigger,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        report=report,
        usage=Usage.model_validate(row.usage) if row.usage else None,
        error=row.error,
        report_invalid=invalid,
        trace_id=row.trace_id,
        gate_path=row.gate_path,
        gate_reason=row.gate_reason,
        user_symptom=row.user_symptom,
    )
