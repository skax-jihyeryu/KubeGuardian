"""이력 질의 대화 저장: 대화 id별 턴을 쌓고 최근 턴을 돌려준다."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.tables import ChatMessageRow, require_aware
from service.model.chat import ChatTurn


class ChatStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def add(
        self,
        conversation_id: str,
        role: str,
        text: str,
        now: datetime,
        answer: dict[str, Any] | None = None,
    ) -> None:
        with Session(self._engine) as s, s.begin():
            s.add(
                ChatMessageRow(
                    conversation_id=conversation_id,
                    role=role,
                    text=text,
                    answer=answer,
                    created_at=require_aware(now),
                )
            )

    def recent(self, conversation_id: str, limit: int = 10) -> list[ChatTurn]:
        """최근 limit개 턴을 오래된 순으로."""
        with Session(self._engine) as s:
            rows = s.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.conversation_id == conversation_id)
                .order_by(ChatMessageRow.id.desc())
                .limit(limit)
            ).all()
        return [ChatTurn(role=cast(Any, r.role), text=r.text) for r in reversed(rows)]
