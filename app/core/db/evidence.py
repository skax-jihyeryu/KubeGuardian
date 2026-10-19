"""Evidence 저장소. id(`ev-0001`)는 여기서 순서대로 발급한다 (점검을 넘어 전체에서 하나)."""

import re

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from core.db.tables import EvidenceRow, require_aware
from service.model import Evidence

_ID = re.compile(r"ev-(\d+)")


def format_evidence_id(pk: int) -> str:
    return f"ev-{pk:04d}"


def _parse_id(ev_id: str) -> int | None:
    m = _ID.fullmatch(ev_id)
    if m is None or format_evidence_id(int(m.group(1))) != ev_id:
        return None  # 발급한 형식(ev-0001)만 받는다. ev-1·ev-00001은 다른 id로 본다
    return int(m.group(1))


class EvidenceStore:
    """check_run_id를 주면 저장하는 Evidence를 그 점검에 묶는다. 데이터는 마스킹된 상태로 받는다."""

    def __init__(self, engine: Engine, check_run_id: int | None = None):
        self._engine = engine
        self._check_run_id = check_run_id

    def add(self, evidence: Evidence) -> str:
        """저장하고 발급한 id를 돌려준다."""
        if evidence.id is not None:
            raise ValueError(f"id는 저장소가 발급한다. 이미 id가 있는 Evidence: {evidence.id}")
        row = EvidenceRow(
            check_run_id=self._check_run_id,
            kind=evidence.kind,
            subject=evidence.subject,
            collected_at=require_aware(evidence.collected_at),
            source=evidence.source,
            data=evidence.data,
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)
            s.flush()
            return format_evidence_id(row.id)

    def get(self, ev_id: str) -> Evidence | None:
        row = self._row(ev_id)
        if row is None:
            return None
        return Evidence(
            id=format_evidence_id(row.id),
            kind=row.kind,
            subject=row.subject,
            collected_at=row.collected_at,
            source=row.source,
            data=row.data,
        )

    def check_run_of(self, ev_id: str) -> int | None:
        row = self._row(ev_id)
        return row.check_run_id if row else None

    def _row(self, ev_id: str) -> EvidenceRow | None:
        pk = _parse_id(ev_id)
        if pk is None:
            return None
        with Session(self._engine) as s:
            return s.get(EvidenceRow, pk)
