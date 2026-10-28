"""알림 이력: 보낸(또는 dry-run으로 기록만 한) 메시지 (기획서 §6.6, UI 정의서 §1.2)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from core.db.tables import AlertRow, require_aware


@dataclass(frozen=True)
class Alert:
    id: int
    incident_id: int | None
    kind: str
    channel: str
    ts: str | None
    thread_ts: str | None
    text: str
    sent_at: datetime
    dry_run: bool
    error: str | None


class AlertStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def add(
        self,
        *,
        kind: str,
        channel: str,
        text: str,
        sent_at: datetime,
        incident_id: int | None = None,
        ts: str | None = None,
        thread_ts: str | None = None,
        dry_run: bool = False,
        error: str | None = None,
    ) -> int:
        row = AlertRow(
            incident_id=incident_id,
            kind=kind,
            channel=channel,
            ts=ts,
            thread_ts=thread_ts,
            text=text,
            sent_at=require_aware(sent_at),
            dry_run=dry_run,
            error=error,
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)
            s.flush()
            return row.id

    def for_incident(self, incident_id: int) -> list[Alert]:
        stmt = select(AlertRow).where(AlertRow.incident_id == incident_id).order_by(AlertRow.id)
        with Session(self._engine) as s:
            return [_to_alert(r) for r in s.scalars(stmt)]

    def count(self, incident_id: int, kind: str) -> int:
        stmt = select(func.count()).where(
            AlertRow.incident_id == incident_id, AlertRow.kind == kind, AlertRow.error.is_(None)
        )
        with Session(self._engine) as s:
            return s.scalar(stmt) or 0

    def last(self, kind: str) -> Alert | None:
        stmt = select(AlertRow).where(AlertRow.kind == kind).order_by(AlertRow.id.desc()).limit(1)
        with Session(self._engine) as s:
            row = s.scalar(stmt)
            return _to_alert(row) if row else None


def _to_alert(row: AlertRow) -> Alert:
    return Alert(
        id=row.id,
        incident_id=row.incident_id,
        kind=row.kind,
        channel=row.channel,
        ts=row.ts,
        thread_ts=row.thread_ts,
        text=row.text,
        sent_at=row.sent_at,
        dry_run=row.dry_run,
        error=row.error,
    )
