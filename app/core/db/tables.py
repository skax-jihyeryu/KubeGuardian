"""SQLite 테이블. W1: 점검 이력·Evidence·관측 기록. 조사 단계·보고서·알림 이력은 W2~W3에 더한다."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def require_aware(value: datetime) -> datetime:
    """시각은 timezone-aware여야 한다. naive는 어느 시간대인지 알 수 없어 거부한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"시각은 timezone-aware여야 한다 (naive datetime): {value!r}")
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """UTC ISO 8601 문자열로 저장하고 UTC aware datetime으로 읽는다."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        return None if value is None else require_aware(value).isoformat()

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


class Base(DeclarativeBase):
    pass


class CheckRunRow(Base):
    __tablename__ = "check_run"
    __table_args__ = {"sqlite_autoincrement": True}  # 지운 id를 다시 쓰지 않는다

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    trigger: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 수집 전에 실패하면 None


class EvidenceRow(Base):
    __tablename__ = "evidence"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)  # 화면·보고서에는 ev-0001로 보인다
    check_run_id: Mapped[int | None] = mapped_column(ForeignKey("check_run.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(512))
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(1024))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ObservationRow(Base):
    __tablename__ = "observation"

    rule_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    subject: Mapped[str] = mapped_column(String(512), primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
