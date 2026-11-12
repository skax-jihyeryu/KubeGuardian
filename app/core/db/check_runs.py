"""점검 이력: 실행 기록과 그때의 스냅샷. 감지기의 `previous`(직전 스냅샷)를 여기서 읽는다."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import JSON, Engine, func, null, or_, select, update
from sqlalchemy.orm import Session

from core.db.tables import (
    CheckResultRow,
    CheckRunRow,
    InvestigationQueueRow,
    InvestigationRow,
    require_aware,
)
from service.model import ClusterSnapshot, Trigger


class CheckRunStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(self, trigger: Trigger, started_at: datetime) -> int:
        row = CheckRunRow(started_at=require_aware(started_at), trigger=trigger)
        with Session(self._engine) as s, s.begin():
            s.add(row)
            s.flush()
            return row.id

    def save_snapshot(self, run_id: int, snapshot: ClusterSnapshot) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(CheckRunRow, run_id)
            row.snapshot = snapshot.model_dump(mode="json")

    def expire_snapshots(self, cutoff: datetime, *, pinned_run_ids: Collection[int] = ()) -> int:
        """기준 시각보다 오래된 스냅샷만 비우고 점검 이력은 보존한다."""
        namespace = CheckRunRow.snapshot["namespace"].as_string()
        latest_by_namespace = (
            select(func.max(CheckRunRow.id)).where(namespace.is_not(None)).group_by(namespace)
        )
        investigations = select(InvestigationRow.check_run_id).where(
            or_(
                InvestigationRow.status == "running",
                InvestigationRow.report != JSON.NULL,
            )
        )
        active_checks = select(CheckResultRow.run_id).where(CheckResultRow.status == "running")
        queued = select(InvestigationQueueRow.check_run_id)
        stmt = (
            update(CheckRunRow)
            .where(
                CheckRunRow.started_at < require_aware(cutoff),
                CheckRunRow.snapshot != JSON.NULL,
                CheckRunRow.id.not_in(latest_by_namespace),
                CheckRunRow.id.not_in(investigations),
                CheckRunRow.id.not_in(active_checks),
                CheckRunRow.id.not_in(queued),
                CheckRunRow.id.not_in(pinned_run_ids),
            )
            .values(snapshot=null())
        )
        with self._engine.begin() as connection:
            return connection.execute(stmt).rowcount

    def get_snapshot(self, run_id: int) -> ClusterSnapshot | None:
        with Session(self._engine) as s:
            row = s.get(CheckRunRow, run_id)
            return _to_snapshot(row.snapshot if row else None)

    def get_pinned_snapshot(self, run_id: int) -> tuple[ClusterSnapshot | None, bool]:
        """명시적으로 지정한 점검의 스냅샷과 점검 완료 여부."""
        stmt = (
            select(CheckRunRow.snapshot, CheckResultRow.status, CheckResultRow.finished_at)
            .outerjoin(CheckResultRow, CheckResultRow.run_id == CheckRunRow.id)
            .where(CheckRunRow.id == run_id)
        )
        with Session(self._engine) as s:
            row = s.execute(stmt).first()
            if row is None:
                return None, False
            return _to_snapshot(row.snapshot), row.status == "done" and row.finished_at is not None

    def latest_started_at(self) -> datetime | None:
        """가장 최근 점검의 시작 시각 (점검 중단 감시, M7)."""
        stmt = select(CheckRunRow.started_at).order_by(CheckRunRow.id.desc()).limit(1)
        with Session(self._engine) as s:
            return s.scalar(stmt)

    def list_runs(
        self, since: datetime | None = None, until: datetime | None = None, limit: int = 20
    ) -> list[tuple[int, datetime, str, bool]]:
        """최신순 점검 목록: (id, 시작 시각, 트리거, 스냅샷 있음). 이력 질의용."""
        stmt = select(
            CheckRunRow.id,
            CheckRunRow.started_at,
            CheckRunRow.trigger,
            CheckRunRow.snapshot != JSON.NULL,
        )
        if since is not None:
            stmt = stmt.where(CheckRunRow.started_at >= require_aware(since))
        if until is not None:
            stmt = stmt.where(CheckRunRow.started_at <= require_aware(until))
        with Session(self._engine) as s:
            rows = s.execute(stmt.order_by(CheckRunRow.id.desc()).limit(limit))
            return [(r[0], r[1], r[2], bool(r[3])) for r in rows]

    def get_run(self, run_id: int) -> tuple[datetime, str] | None:
        """점검의 (시작 시각, 트리거). 없으면 None."""
        with Session(self._engine) as s:
            row = s.get(CheckRunRow, run_id)
            return (row.started_at, row.trigger) if row else None

    def exists(self, run_id: int) -> bool:
        with Session(self._engine) as s:
            return s.get(CheckRunRow, run_id) is not None

    def latest_run(self) -> tuple[int, datetime] | None:
        stmt = (
            select(CheckRunRow.id, CheckRunRow.started_at).order_by(CheckRunRow.id.desc()).limit(1)
        )
        with Session(self._engine) as s:
            row = s.execute(stmt).first()
            return (row[0], row[1]) if row else None

    def previous_snapshot(self, run_id: int, namespace: str) -> ClusterSnapshot | None:
        """run_id보다 앞선 점검 중 같은 네임스페이스의 스냅샷이 있는 가장 최근 것. 없으면 None.

        대상 네임스페이스를 바꾼 뒤 다른 네임스페이스의 값과 비교하지 않도록 거른다.
        """
        stmt = (
            select(CheckRunRow.snapshot)
            .where(
                CheckRunRow.id < run_id,
                CheckRunRow.snapshot.is_not(None),
                CheckRunRow.snapshot["namespace"].as_string() == namespace,
            )
            .order_by(CheckRunRow.id.desc())
            .limit(1)
        )
        with Session(self._engine) as s:
            return _to_snapshot(s.scalar(stmt))


def _to_snapshot(data: dict | None) -> ClusterSnapshot | None:
    return None if data is None else ClusterSnapshot.model_validate(data)
