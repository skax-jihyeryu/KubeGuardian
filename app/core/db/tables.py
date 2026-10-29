"""SQLite 테이블. W1: 점검 이력·Evidence·관측 기록. W3: 점검 잠금·건너뜀, 이후 보고서·알림 이력."""

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


class CheckLockRow(Base):
    """점검 동시 실행 1건 잠금 (UI 정의서 §6.5). 행은 id=1 하나만 쓴다."""

    __tablename__ = "check_lock"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32))  # 잡은 쪽만 놓을 수 있게 하는 값
    trigger: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[int | None] = mapped_column()  # 점검 이력을 만든 뒤 붙인다
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CheckSkipRow(Base):
    """잠금 때문에 건너뛴 점검. check_run에 열을 더하지 않는다 (기존 DB에 열이 추가되지 않음)."""

    __tablename__ = "check_skip"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    skipped_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    holder_trigger: Mapped[str] = mapped_column(String(16))
    holder_run_id: Mapped[int | None] = mapped_column()


class IncidentRow(Base):
    """이슈 (기획서 §6.6). 장애 신호·증상이 이어지는 동안 하나로 묶이는 단위. 화면 용어는 이슈."""

    __tablename__ = "incident"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # open / resolved
    fingerprint: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)  # 지문 구성 요소·영향 기능·해결 판정 대상
    first_severity: Mapped[str | None] = mapped_column(String(16))  # 1차 (3값)
    final_severity: Mapped[str | None] = mapped_column(String(32))  # 보고서 impact (4값)
    investigation: Mapped[str | None] = mapped_column(String(16))  # pending → (작업자가) done 등
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # #27
    slack_ts: Mapped[str | None] = mapped_column(String(32))  # #27: 1차 알림 메시지 ID


class IncidentCheckRow(Base):
    """점검 ↔ 이슈 (다대다). action: opened / continued / changed / seen / resolved."""

    __tablename__ = "incident_check"

    incident_id: Mapped[int] = mapped_column(ForeignKey("incident.id"), primary_key=True)
    check_run_id: Mapped[int] = mapped_column(primary_key=True, index=True)
    action: Mapped[str] = mapped_column(String(16))


class AlertRow(Base):
    """알림 이력 (기획서 §6.6). dry-run이면 보내지 않고 본문만 남긴다. 평가 러너가 시각을 읽는다."""

    __tablename__ = "alert"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # M1 / M1u(갱신) / M9 / M2 / M3 / M7
    channel: Mapped[str] = mapped_column(String(128))
    ts: Mapped[str | None] = mapped_column(String(32))  # Slack 메시지 ID (실패면 None)
    thread_ts: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    dry_run: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(String(512))


class InvestigationQueueRow(Base):
    """조사 대기열: 이슈마다 최신 조사 요청 하나 (점검 시점의 신호판·토폴로지, #28).

    diagnostic-agent 작업자가 claim으로 집고, 끝나면 지운다. 지문이 바뀌면 새 요청으로 덮는다.
    """

    __tablename__ = "investigation_queue"

    incident_id: Mapped[int] = mapped_column(ForeignKey("incident.id"), primary_key=True)
    check_run_id: Mapped[int] = mapped_column()  # 요청을 만든 점검 (근거가 이 점검에 쌓인다)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)  # InvestigationRequest
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(32))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class InvestigationRow(Base):
    """AI 조사 한 번 (보고서 화면의 조사·보고서, 상세설계 §2.2). 끝나면 보고서가 붙는다."""

    __tablename__ = "investigation"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    check_run_id: Mapped[int] = mapped_column(ForeignKey("check_run.id"), index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"), index=True)
    run_key: Mapped[str] = mapped_column(String(128))  # RunContext.run_id (추적·로그와 맞춤)
    agent: Mapped[str] = mapped_column(String(64))  # investigator:v1
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # running / done / incomplete / failed
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(1024))


class AgentStepRow(Base):
    """조사 단계 하나. 단계가 끝날 때마다 바로 쓴다 (진행 중 화면 갱신, UI 정의서 §6.3)."""

    __tablename__ = "agent_step"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    investigation_id: Mapped[int] = mapped_column(ForeignKey("investigation.id"), index=True)
    node: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tool: Mapped[str | None] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 마스킹된 값
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 마스킹된 값
