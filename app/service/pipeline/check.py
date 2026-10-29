"""점검 실행 흐름 v0: 수집 → 점검 이력·스냅샷 저장 → 규칙 점검 → 게이트 → 근거 저장.

http가 있고 설정에 API 시나리오가 있으면 규칙 점검 뒤에 증상 감지(경로 B)를 돌린다.
게이트 뒤에 이슈(incident)를 열고·잇고·해결한다 (service/alerts). 조사는 여기서 하지 않는다.
조사, 알림은 W2~W3에 이 흐름 뒤에 붙인다.
점검은 실행 경로와 상관없이 동시에 1건이다 (UI 정의서 §6.5). 시작할 때 공유 잠금(점검 DB)을 잡고
끝나면(실패 포함) 놓는다. 잡혀 있으면 CHECK_ALREADY_RUNNING이고, 주기 점검이면 건너뜀을 기록한다.
화면·CLI 점검은 CronJob처럼 시간 제한으로 잘리지 않으므로 단계마다 잠금 만료를 늘린다.
"""

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.config import KubeGuardianConfig
from core.db import (
    CheckLockStore,
    CheckRunStore,
    EvidenceStore,
    InvestigationQueue,
    SqliteObservationStore,
)
from core.db.check_lock import CheckLock
from core.error.service_exception import ErrorCode, ServiceException
from service.alerts.incidents import IncidentChange, track
from service.checks.detectors.base import DetectorContext
from service.checks.gate import decide_gate
from service.checks.runner import run_detectors
from service.checks.signal_board import build_signal_board
from service.checks.symptoms import detect_symptoms
from service.model import (
    ClusterSnapshot,
    Evidence,
    GateResult,
    Signal,
    SignalBoard,
    Symptom,
    Trigger,
)
from service.sensors.collector import KubeReader, collect
from service.tools.base import ToolContext

# 점검 잠금 만료. CronJob activeDeadlineSeconds(270)와 같게 둬서
# 잘린 점검의 잠금이 다음 회차(300초) 전에 풀린다
CHECK_LOCK_TTL = timedelta(seconds=270)


def _wall_now() -> datetime:
    """잠금 만료를 늘릴 때의 실제 시각 (점검 기준 시각 now와 다를 수 있다)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _HeldLock:
    locks: CheckLockStore
    token: str

    def renew(self) -> None:
        with suppress(SQLAlchemyError):  # 못 늘리면 원래 만료를 따른다
            self.locks.renew(self.token, _wall_now(), CHECK_LOCK_TTL)


def clean_symptom(text: str | None) -> str | None:
    """사용자 증상의 앞뒤 공백을 떼고, 비면 None (공백뿐인 입력은 증상이 아니다)."""
    return (text or "").strip() or None


@dataclass(frozen=True)
class CheckOutcome:
    run_id: int
    snapshot: ClusterSnapshot
    signals: list[Signal]  # 규칙 목록 순서
    gate: GateResult
    board: SignalBoard
    symptoms: list[Symptom]  # 실행한 시나리오 전부 (실패한 것이 증상). http가 없으면 빈 목록
    incidents: list[IncidentChange] = field(default_factory=list)  # 이번 점검이 바꾼 이슈 상태


def run_check(
    reader: KubeReader,
    config: KubeGuardianConfig,
    engine: Engine,
    *,
    trigger: Trigger,
    now: datetime | None = None,
    http: Any = None,  # core.http 클라이언트. 없으면 증상 감지를 건너뛴다
    user_symptom: str | None = None,
) -> CheckOutcome:
    now = now or datetime.now(UTC)
    lock = _acquire_lock(CheckLockStore(engine), trigger, now)
    try:
        return _run_locked(
            reader,
            config,
            engine,
            lock=lock,
            trigger=trigger,
            now=now,
            http=http,
            user_symptom=user_symptom,
        )
    finally:
        with suppress(SQLAlchemyError):  # 못 놓으면 만료(CHECK_LOCK_TTL) 뒤 다음 점검이 가져간다
            lock.locks.release(lock.token)


def _acquire_lock(locks: CheckLockStore, trigger: Trigger, now: datetime) -> _HeldLock:
    try:
        got = locks.acquire(trigger, now, CHECK_LOCK_TTL)
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    if not got.acquired:
        if trigger == "schedule":
            # 기록에 실패해도 조용한 건너뜀(CHECK_ALREADY_RUNNING)으로 끝낸다
            with suppress(SQLAlchemyError):
                locks.record_skip(trigger, now, got.holder)
        raise ServiceException(ErrorCode.CHECK_ALREADY_RUNNING, _holder_text(got.holder))
    return _HeldLock(locks, got.holder.token)


def _holder_text(holder: CheckLock) -> str:
    run = f" run {holder.run_id}" if holder.run_id is not None else ""
    return (
        f"진행 중인 점검: {holder.trigger}{run}, "
        f"{holder.acquired_at.isoformat()} 시작 (만료 {holder.expires_at.isoformat()})"
    )


def _run_locked(
    reader: KubeReader,
    config: KubeGuardianConfig,
    engine: Engine,
    *,
    lock: _HeldLock,
    trigger: Trigger,
    now: datetime,
    http: Any,
    user_symptom: str | None,
) -> CheckOutcome:
    try:
        runs = CheckRunStore(engine)
        run_id = runs.create(trigger, now)
        lock.locks.attach_run(lock.token, run_id)
        snapshot = collect(reader, now=now)
        runs.save_snapshot(run_id, snapshot)
        lock.renew()
        ctx = DetectorContext(
            snapshot=snapshot,
            previous=runs.previous_snapshot(run_id, snapshot.namespace),
            observations=SqliteObservationStore(engine),
            config=config,
            now=now,
        )
        evidence = EvidenceStore(engine, run_id)
        signals = _store_evidence(evidence, snapshot, run_detectors(ctx))
        lock.renew()
        symptoms = []
        if http is not None and config.api_scenarios:
            tool_ctx = ToolContext(
                namespace=config.target.namespace,
                evidence=evidence,
                k8s=reader,
                now=lambda: now,
                http=http,
            )
            symptoms = detect_symptoms(config, tool_ctx)
        gate = decide_gate(signals, symptoms, clean_symptom(user_symptom))
        # 이슈 상태도 잠금 안에서 정한다 (두 점검이 같은 이슈를 동시에 열지 않게)
        incidents = track(
            engine,
            config,
            run_id=run_id,
            trigger=trigger,
            now=now,
            gate=gate,
            signals=signals,
            symptoms=symptoms,
        )
        outcome = CheckOutcome(
            run_id=run_id,
            snapshot=snapshot,
            signals=signals,
            gate=gate,
            board=build_signal_board(signals),
            symptoms=symptoms,
            incidents=incidents,
        )
        _queue_investigations(engine, config, outcome, trigger, now)
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    return outcome


def _queue_investigations(
    engine: Engine,
    config: KubeGuardianConfig,
    outcome: CheckOutcome,
    trigger: Trigger,
    now: datetime,
) -> None:
    """새 이슈·지문 변경이면 점검 시점의 조사 요청을 대기열에 넣는다 (작업자가 집는다, #28)."""
    from service.pipeline.investigate import build_request  # investigate가 이 모듈을 import한다

    queue = InvestigationQueue(engine)
    for change in outcome.incidents:
        if change.needs_investigation:
            request = build_request(outcome, config, trigger=trigger)
            if request is not None:
                queue.put(change.incident_id, outcome.run_id, request.model_dump(mode="json"), now)


def _store_evidence(
    store: EvidenceStore, snapshot: ClusterSnapshot, signals: list[Signal]
) -> list[Signal]:
    """적발된 신호마다 판정에 쓴 값을 근거로 저장하고 evidence_id를 붙인다."""
    out = []
    for s in signals:
        if s.verdict == "detected":
            ev_id = store.add(
                Evidence(
                    kind="detector",
                    subject=s.subject,
                    collected_at=snapshot.collected_at,
                    source=f"detector {s.rule_id}",
                    data=s.model_dump(mode="json", exclude={"evidence_id"}),
                )
            )
            s = s.model_copy(update={"evidence_id": ev_id})
        out.append(s)
    return out
