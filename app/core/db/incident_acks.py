"""이슈 확인 기록: 긴급 알림의 Slack 확인 버튼을 누른 사람 (#156). 이슈마다 첫 확인만 남긴다."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.db.tables import IncidentAckRow, require_aware


@dataclass(frozen=True)
class IncidentAck:
    incident_id: int
    user_id: str
    user_name: str
    acked_at: datetime


class IncidentAckStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def ack(self, incident_id: int, user_id: str, user_name: str, now: datetime) -> bool:
        """확인을 기록한다. 이슈의 첫 확인이면 True, 이미 있으면 기록을 바꾸지 않고 False."""
        stmt = (
            insert(IncidentAckRow)
            .values(
                incident_id=incident_id,
                user_id=user_id,
                user_name=user_name,
                acked_at=require_aware(now),
            )
            .on_conflict_do_nothing()
        )
        with Session(self._engine) as s, s.begin():
            return s.execute(stmt).rowcount == 1

    def get(self, incident_id: int) -> IncidentAck | None:
        with Session(self._engine) as s:
            row = s.get(IncidentAckRow, incident_id)
            if row is None:
                return None
            return IncidentAck(row.incident_id, row.user_id, row.user_name, row.acked_at)
