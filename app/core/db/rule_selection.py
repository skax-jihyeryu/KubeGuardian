"""점검 규칙 선택. 행이 없는 규칙은 켜진 것이다 (새 DB·평가 DB는 전부 켜짐, #116)."""

from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.tables import RuleSelectionRow, require_aware


class RuleSelectionStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def disabled(self) -> set[str]:
        stmt = select(RuleSelectionRow.rule_id).where(RuleSelectionRow.enabled.is_(False))
        with Session(self._engine) as s:
            return set(s.scalars(stmt))

    def save(self, enabled: set[str], known: set[str], now: datetime) -> None:
        """known(고를 수 있는 규칙) 중 enabled만 켜고 나머지는 끈다. known 밖 ID는 ValueError."""
        bad = sorted(enabled - known)
        if bad:
            raise ValueError(f"고를 수 없는 규칙: {', '.join(bad)}")
        now = require_aware(now)
        with Session(self._engine) as s, s.begin():
            for rule_id in known:
                s.merge(
                    RuleSelectionRow(rule_id=rule_id, enabled=rule_id in enabled, updated_at=now)
                )
