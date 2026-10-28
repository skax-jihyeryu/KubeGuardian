"""점검 실행 흐름 v0: 수집 → 점검 이력·스냅샷 저장 → 규칙 점검 → 게이트 → 근거 저장.

http가 있고 설정에 API 시나리오가 있으면 규칙 점검 뒤에 증상 감지(경로 B)를 돌린다.
조사, 알림은 W2~W3에 이 흐름 뒤에 붙인다.
점검은 실행 경로와 상관없이 동시에 1건이다 (UI 정의서 §6.5). 시작할 때 공유 잠금(점검 DB)을 잡고
끝나면(실패 포함) 놓는다. 잡혀 있으면 CHECK_ALREADY_RUNNING이고, 주기 점검이면 건너뜀을 기록한다.
"""

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.config import KubeGuardianConfig
from core.db import CheckLockStore, CheckRunStore, EvidenceStore, SqliteObservationStore
from core.db.check_lock import CheckLock
from core.error.service_exception import ErrorCode, ServiceException
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
    token = _acquire_lock(engine, trigger, now)
    try:
        return _run_locked(reader, config, engine, token, trigger, now, http, user_symptom)
    finally:
        with suppress(SQLAlchemyError):  # 못 놓으면 만료(CHECK_LOCK_TTL) 뒤 다음 점검이 가져간다
            CheckLockStore(engine).release(token)


def _acquire_lock(engine: Engine, trigger: Trigger, now: datetime) -> str:
    locks = CheckLockStore(engine)
    try:
        got = locks.acquire(trigger, now, CHECK_LOCK_TTL)
        if not got.acquired and trigger == "schedule":
            locks.record_skip(trigger, now, got.holder)
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    if not got.acquired:
        raise ServiceException(ErrorCode.CHECK_ALREADY_RUNNING, _holder_text(got.holder))
    return got.holder.token


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
    token: str,
    trigger: Trigger,
    now: datetime,
    http: Any,
    user_symptom: str | None,
) -> CheckOutcome:
    try:
        runs = CheckRunStore(engine)
        run_id = runs.create(trigger, now)
        CheckLockStore(engine).attach_run(token, run_id)
        snapshot = collect(reader, now=now)
        runs.save_snapshot(run_id, snapshot)
        ctx = DetectorContext(
            snapshot=snapshot,
            previous=runs.previous_snapshot(run_id, snapshot.namespace),
            observations=SqliteObservationStore(engine),
            config=config,
            now=now,
        )
        evidence = EvidenceStore(engine, run_id)
        signals = _store_evidence(evidence, snapshot, run_detectors(ctx))
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
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    return CheckOutcome(
        run_id=run_id,
        snapshot=snapshot,
        signals=signals,
        gate=decide_gate(signals, symptoms, clean_symptom(user_symptom)),
        board=build_signal_board(signals),
        symptoms=symptoms,
    )


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
