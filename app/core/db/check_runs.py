"""점검 이력: 실행 기록과 그때의 스냅샷. 감지기의 `previous`(직전 스냅샷)를 여기서 읽는다."""

from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.tables import CheckRunRow, require_aware
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

    def get_snapshot(self, run_id: int) -> ClusterSnapshot | None:
        with Session(self._engine) as s:
            row = s.get(CheckRunRow, run_id)
            return _to_snapshot(row.snapshot if row else None)

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
