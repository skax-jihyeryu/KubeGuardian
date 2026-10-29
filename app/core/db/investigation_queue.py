"""조사 대기열 (#28). 점검이 연·바꾼 이슈의 조사 요청을 두고, 작업자가 하나씩 집는다.

- 집기(claim)는 "아무도 안 집었거나 집은 지 오래됨이면 내 것으로"를 한 문장 UPDATE로 해
  여러 작업자(또는 CLI)가 같은 이슈를 동시에 조사하지 않는다. 죽은 작업자의 이슈는 다시 집힌다.
- 끝내기(finish)는 token이 그대로일 때만 된다. 조사 중에 지문이 바뀌어 새 요청이 들어왔거나
  만료로 남이 가져갔으면 False를 돌려주고, 옛 조사 결과는 남기지 않는다.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.db.tables import IncidentRow, InvestigationQueueRow, require_aware


@dataclass(frozen=True)
class Job:
    incident_id: int
    check_run_id: int
    request: dict[str, Any]
    token: str


class InvestigationQueue:
    def __init__(self, engine: Engine):
        self._engine = engine

    def put(
        self, incident_id: int, check_run_id: int, request: dict[str, Any], now: datetime
    ) -> None:
        """이슈의 조사 요청을 넣는다. 이미 있으면(지문 변경) 새 요청으로 덮고 집힘을 푼다."""
        values = {
            "incident_id": incident_id,
            "check_run_id": check_run_id,
            "request": request,
            "queued_at": require_aware(now),
            "claim_token": None,
            "claimed_at": None,
        }
        stmt = insert(InvestigationQueueRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["incident_id"],
            set_={k: stmt.excluded[k] for k in values if k != "incident_id"},
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)
            s.get_one(IncidentRow, incident_id).investigation = "pending"

    def claim(
        self, now: datetime, stale: timedelta, *, incident_id: int | None = None
    ) -> Job | None:
        """대기 중(또는 집은 지 stale 넘은)인 열린 이슈의 요청을 하나 집는다. 오래된 것부터."""
        now = require_aware(now)
        cutoff = (now - stale).isoformat()
        free = or_(
            InvestigationQueueRow.claimed_at.is_(None),
            func.julianday(InvestigationQueueRow.claimed_at) <= func.julianday(cutoff),
        )
        candidates = (
            select(InvestigationQueueRow.incident_id)
            .join(IncidentRow, IncidentRow.id == InvestigationQueueRow.incident_id)
            .where(IncidentRow.status == "open", free)
            .order_by(InvestigationQueueRow.queued_at)
        )
        if incident_id is not None:
            candidates = candidates.where(InvestigationQueueRow.incident_id == incident_id)
        with Session(self._engine) as s:
            ids = list(s.scalars(candidates))
        for iid in ids:
            token = secrets.token_hex(16)
            stmt = (
                update(InvestigationQueueRow)
                .where(InvestigationQueueRow.incident_id == iid, free)
                .values(claim_token=token, claimed_at=now)
            )
            with Session(self._engine) as s, s.begin():
                if s.execute(stmt).rowcount == 0:
                    continue  # 다른 작업자가 먼저 집었다
                s.get_one(IncidentRow, iid).investigation = "running"
                row = s.get_one(InvestigationQueueRow, iid)
                return Job(iid, row.check_run_id, dict(row.request), token)
        return None

    def finish(self, job: Job) -> bool:
        """아직 내 것이면 대기열에서 지우고 True. 새 요청·다른 작업자가 가져갔으면 False."""
        stmt = delete(InvestigationQueueRow).where(
            InvestigationQueueRow.incident_id == job.incident_id,
            InvestigationQueueRow.claim_token == job.token,
        )
        with Session(self._engine) as s, s.begin():
            return s.execute(stmt).rowcount > 0
