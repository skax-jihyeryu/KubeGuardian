"""근거별 요약 캐시와 생성 권한. LLM 호출 동안 DB 잠금을 유지하지 않는다."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.db.evidence import _parse_id, format_evidence_id
from core.db.tables import EvidenceRow, EvidenceSummaryRow, require_aware

PENDING_TTL = timedelta(seconds=90)  # LLM 단일 요청 제한(30초)보다 길다


@dataclass(frozen=True)
class EvidenceSummary:
    status: str
    text: str | None
    model: str
    truncated: bool


class EvidenceSummaryStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, ev_id: str, now: datetime) -> EvidenceSummary | None:
        with Session(self._engine) as s:
            row = s.get(EvidenceSummaryRow, _parse_id(ev_id))
            if row is None:
                return None
            expired = row.status == "pending" and now - row.updated_at >= PENDING_TTL
            return EvidenceSummary(
                "failed" if expired else row.status, row.text, row.model, row.truncated
            )

    def pending_evidence_ids(
        self, since: datetime, limit: int, before_id: int | None = None
    ) -> list[str]:
        """요약 행이 없는 근거를 최신순으로. before_id보다 작은 번호만 본다 (쪽 넘김)."""
        stmt = (
            select(EvidenceRow.id)
            .outerjoin(EvidenceSummaryRow, EvidenceSummaryRow.evidence_id == EvidenceRow.id)
            .where(EvidenceSummaryRow.evidence_id.is_(None), EvidenceRow.collected_at >= since)
            .order_by(EvidenceRow.id.desc())
            .limit(limit)
        )
        if before_id is not None:
            stmt = stmt.where(EvidenceRow.id < before_id)
        with Session(self._engine) as s:
            return [format_evidence_id(pk) for pk in s.scalars(stmt)]

    def claim(self, ev_id: str, model: str, now: datetime, *, retry: bool = False) -> str | None:
        """미생성 또는 명시적 재시도만 허용한다. 조건부 upsert로 프로세스 간 중복을 막는다."""
        pk = _parse_id(ev_id)
        if pk is None:
            raise ValueError("잘못된 근거 번호")
        token = uuid4().hex
        values = dict(
            evidence_id=pk,
            status="pending",
            text=None,
            model=model,
            token=token,
            updated_at=require_aware(now),
            truncated=False,
        )
        stmt = insert(EvidenceSummaryRow).values(**values)
        if retry:
            stmt = stmt.on_conflict_do_update(
                index_elements=[EvidenceSummaryRow.evidence_id],
                set_=values,
                where=(EvidenceSummaryRow.status == "failed")
                | (
                    (EvidenceSummaryRow.status == "pending")
                    & (EvidenceSummaryRow.updated_at <= now - PENDING_TTL)
                ),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=[EvidenceSummaryRow.evidence_id])
        with Session(self._engine) as s, s.begin():
            return token if s.execute(stmt).rowcount else None

    def finish(
        self, ev_id: str, token: str, text: str | None, truncated: bool, now: datetime
    ) -> None:
        stmt = (
            update(EvidenceSummaryRow)
            .where(
                EvidenceSummaryRow.evidence_id == _parse_id(ev_id),
                EvidenceSummaryRow.token == token,
                EvidenceSummaryRow.status == "pending",
            )
            .values(
                status="done" if text else "failed",
                text=text,
                truncated=truncated,
                updated_at=require_aware(now),
            )
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)
