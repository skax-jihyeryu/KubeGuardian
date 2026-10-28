"""이슈(incident) 저장소: 열림·해결 상태, 지문, 심각도, 조사 대기, 점검과의 연결 (기획서 §6.6).

열고·잇고·해결하는 판단은 `service/alerts/incidents.py`가 하고, 여기는 읽고 쓰기만 한다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.tables import IncidentCheckRow, IncidentRow, require_aware


@dataclass(frozen=True)
class Incident:
    id: int
    status: str
    fingerprint: str
    path: str
    rule_ids: tuple[str, ...]
    failed_steps: tuple[str, ...]
    services: tuple[str, ...]
    features: tuple[str, ...]
    targets: tuple[tuple[str, str], ...]
    scenarios: tuple[str, ...]
    first_severity: str | None
    final_severity: str | None
    investigation: str | None
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class IncidentStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def open(
        self,
        fingerprint: str,
        detail: dict[str, Any],
        severity: str | None,
        run_id: int,
        now: datetime,
    ) -> int:
        now = require_aware(now)
        row = IncidentRow(
            status="open",
            fingerprint=fingerprint,
            detail=detail,
            first_severity=severity,
            investigation="pending",
            opened_at=now,
            last_seen_at=now,
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)
            s.flush()
            s.add(IncidentCheckRow(incident_id=row.id, check_run_id=run_id, action="opened"))
            return row.id

    def change(
        self,
        incident_id: int,
        fingerprint: str,
        detail: dict[str, Any],
        severity: str | None,
        run_id: int,
        now: datetime,
    ) -> None:
        """지문이 바뀜: 같은 이슈에서 재조사한다 (조사 대기로 되돌린다)."""
        with Session(self._engine) as s, s.begin():
            row = s.get_one(IncidentRow, incident_id)
            row.fingerprint, row.detail, row.first_severity = fingerprint, detail, severity
            row.investigation = "pending"
            row.last_seen_at = require_aware(now)
            s.add(IncidentCheckRow(incident_id=incident_id, check_run_id=run_id, action="changed"))

    def touch(self, incident_id: int, run_id: int, now: datetime, action: str) -> None:
        """같은 지문이 이어짐(continued) 또는 직접 점검이 본 것(seen)."""
        with Session(self._engine) as s, s.begin():
            row = s.get_one(IncidentRow, incident_id)
            row.last_seen_at = require_aware(now)
            s.add(IncidentCheckRow(incident_id=incident_id, check_run_id=run_id, action=action))

    def resolve(self, incident_id: int, run_id: int, now: datetime) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(IncidentRow, incident_id)
            row.status, row.resolved_at = "resolved", require_aware(now)
            s.add(IncidentCheckRow(incident_id=incident_id, check_run_id=run_id, action="resolved"))

    def set_final_severity(self, incident_id: int, impact: str) -> None:
        with Session(self._engine) as s, s.begin():
            s.get_one(IncidentRow, incident_id).final_severity = impact

    def set_investigation(self, incident_id: int, status: str | None) -> None:
        with Session(self._engine) as s, s.begin():
            s.get_one(IncidentRow, incident_id).investigation = status

    def get(self, incident_id: int) -> Incident | None:
        with Session(self._engine) as s:
            row = s.get(IncidentRow, incident_id)
            return _to_incident(row) if row else None

    def open_incidents(self) -> list[Incident]:
        return self._list(select(IncidentRow).where(IncidentRow.status == "open"))

    def pending_investigations(self) -> list[Incident]:
        """조사 대기 중인 열린 이슈 (diagnostic-agent 작업자가 집는다, #28)."""
        stmt = select(IncidentRow).where(
            IncidentRow.status == "open", IncidentRow.investigation == "pending"
        )
        return self._list(stmt)

    def check_ids(self, incident_id: int) -> list[int]:
        stmt = (
            select(IncidentCheckRow.check_run_id)
            .where(IncidentCheckRow.incident_id == incident_id)
            .order_by(IncidentCheckRow.check_run_id)
        )
        with Session(self._engine) as s:
            return list(s.scalars(stmt))

    def _list(self, stmt) -> list[Incident]:
        with Session(self._engine) as s:
            return [_to_incident(r) for r in s.scalars(stmt.order_by(IncidentRow.id))]


def _to_incident(row: IncidentRow) -> Incident:
    d = row.detail or {}
    return Incident(
        id=row.id,
        status=row.status,
        fingerprint=row.fingerprint,
        path=d.get("path", ""),
        rule_ids=tuple(d.get("rule_ids", [])),
        failed_steps=tuple(d.get("failed_steps", [])),
        services=tuple(d.get("services", [])),
        features=tuple(d.get("features", [])),
        targets=tuple(tuple(t) for t in d.get("targets", [])),
        scenarios=tuple(d.get("scenarios", [])),
        first_severity=row.first_severity,
        final_severity=row.final_severity,
        investigation=row.investigation,
        opened_at=row.opened_at,
        last_seen_at=row.last_seen_at,
        resolved_at=row.resolved_at,
    )
