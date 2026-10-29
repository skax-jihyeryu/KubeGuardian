"""조사 기록: 조사 한 번과 그 단계·보고서 (보고서 화면 §6.4 조사, #23).

- 단계는 끝날 때마다 바로 쓴다(SqliteSteps). 진행 중인 조사의 단계를 화면이 수 초마다 다시 읽는다.
- 끝나면 결과(상태·보고서·사용량·오류)를 같은 행에 남긴다. 단계 값은 에이전트가 이미 마스킹했다.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.tables import AgentStepRow, InvestigationRow, require_aware
from service.agent.base import AgentResult, AgentStep, MemorySteps, Usage
from service.model import Report


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
        with Session(self._engine) as s, s.begin():
            s.add(row)


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
    ) -> int:
        row = InvestigationRow(
            check_run_id=check_run_id,
            incident_id=incident_id,
            run_key=run_key,
            agent=agent,
            trigger=trigger,
            status="running",
            started_at=require_aware(now),
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)
            s.flush()
            return row.id

    def steps_sink(self, investigation_id: int) -> SqliteSteps:
        return SqliteSteps(self._engine, investigation_id)

    def finish(self, investigation_id: int, result: AgentResult, now: datetime) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(InvestigationRow, investigation_id)
            row.status = result.status
            row.ended_at = require_aware(now)
            row.report = result.report.model_dump(mode="json") if result.report else None
            row.usage = result.usage.model_dump(mode="json")
            row.error = result.error

    def get(self, investigation_id: int) -> Investigation | None:
        with Session(self._engine) as s:
            row = s.get(InvestigationRow, investigation_id)
            return _to_investigation(row) if row else None

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
        report=Report.model_validate(row.report) if row.report else None,
        usage=Usage.model_validate(row.usage) if row.usage else None,
        error=row.error,
    )
