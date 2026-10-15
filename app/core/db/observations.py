"""관측 기록 저장소 (05-rules §0.2).

`ObservationStore`(service/checks/detectors/base.py)의 SQLite 구현이다.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.db.tables import ObservationRow, require_aware
from service.checks.detectors.base import Observation


class SqliteObservationStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, rule_id: str, subject: str) -> Observation | None:
        with Session(self._engine) as s:
            row = s.get(ObservationRow, (rule_id, subject))
            return _to_observation(row) if row else None

    def touch(
        self, rule_id: str, subject: str, now: datetime, values: dict[str, Any] | None = None
    ) -> Observation:
        now = require_aware(now)
        # 한 문장 upsert: 두 프로세스(CronJob·화면 점검)가 동시에 처음 기록해도 충돌하지 않는다
        stmt = insert(ObservationRow).values(
            rule_id=rule_id, subject=subject, first_seen=now, last_seen=now, values=values or {}
        )
        update: dict[str, Any] = {"last_seen": stmt.excluded.last_seen}
        if values is not None:
            update["values"] = stmt.excluded["values"]
        stmt = stmt.on_conflict_do_update(index_elements=["rule_id", "subject"], set_=update)
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)
            row = s.get_one(ObservationRow, (rule_id, subject))
            return _to_observation(row)

    def clear(self, rule_id: str, subject: str) -> None:
        stmt = delete(ObservationRow).where(
            ObservationRow.rule_id == rule_id, ObservationRow.subject == subject
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)

    def prune(self, rule_id: str, keep_subjects: set[str]) -> None:
        stmt = delete(ObservationRow).where(
            ObservationRow.rule_id == rule_id, ObservationRow.subject.not_in(keep_subjects)
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)


def _to_observation(row: ObservationRow) -> Observation:
    return Observation(
        first_seen=require_aware(row.first_seen),
        last_seen=require_aware(row.last_seen),
        values=dict(row.values or {}),
    )
