"""점검 실행 흐름 v0: 수집 → 점검 이력·스냅샷 저장 → 규칙 점검 → 게이트 → 근거 저장.

http가 있고 설정에 API 시나리오가 있으면 규칙 점검 뒤에 증상 감지(경로 B)를 돌린다.
게이트 뒤에 이슈(incident)를 열고·잇고·해결한다 (service/alerts). 조사는 여기서 하지 않는다.
조사, 알림은 W2~W3에 이 흐름 뒤에 붙인다.
점검은 실행 경로와 상관없이 동시에 1건이다 (UI 정의서 §6.5). 시작할 때 공유 잠금(점검 DB)을 잡고
끝나면(실패 포함) 놓는다. 잡혀 있으면 CHECK_ALREADY_RUNNING이고, 주기 점검이면 건너뜀을 기록한다.
화면·CLI 점검은 CronJob처럼 시간 제한으로 잘리지 않으므로 단계마다 잠금 만료를 늘린다.
"""

from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.config import KubeGuardianConfig
from core.db import (
    CheckLockStore,
    CheckResultStore,
    CheckRunStore,
    EvidenceStore,
    RuleSelectionStore,
    SqliteObservationStore,
)
from core.db.check_lock import CHECK_LOCK_TTL, CheckLock
from core.db.investigations import INTERRUPTED
from core.error.service_exception import ErrorCode, ServiceException
from core.log.logging import get_logging
from service.alerts.incidents import IncidentChange, track
from service.checks.detectors.base import DetectorContext
from service.checks.detectors.catalog import CATALOG
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

logger = get_logging()


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


def _excluded_rules(engine: Engine) -> frozenset[str]:
    """끈 규칙 (규칙 선택, #116). 못 읽으면 전부 켠 채로 점검한다."""
    try:
        return frozenset(RuleSelectionStore(engine).disabled() & set(CATALOG))
    except SQLAlchemyError as e:
        logger.warning(f"규칙 선택 읽기 실패: {type(e).__name__}")
        return frozenset()


class _Recorder:
    """점검 결과(check_result) 기록. 부가 작업이라 실패는 로그만 남기고 점검을 막지 않는다 (#116).

    시각은 점검 기준 시각 now에서 실제 흐른 시간만큼 더한 값이다 (평가의 고정 now와도 맞는다).
    """

    def __init__(self, engine: Engine, now: datetime):
        self._store = CheckResultStore(engine)
        self._now = now
        self._t0 = _wall_now()
        self._run_id: int | None = None
        self._phase_start = now
        self.gate: GateResult | None = None
        self.signals: list[Signal] = []
        self.symptoms: list[Symptom] = []
        self.rules: list[str] = []  # 돌린 규칙 (규칙 목록 순서)

    def clock(self) -> datetime:
        return self._now + (_wall_now() - self._t0)

    def start(self, run_id: int, excluded: frozenset[str]) -> None:
        self._run_id = run_id
        self._record(lambda: self._store.start(run_id, self._now, sorted(excluded)))
        self._phase_start = self.clock()

    def phase(self, name: str, summary: Callable[[], str]) -> None:
        ended = self.clock()
        started, self._phase_start = self._phase_start, ended
        if self._run_id is not None:
            run_id = self._run_id
            self._record(lambda: self._store.phase(run_id, name, started, ended, summary()))

    def finish(self, status: str, error: str | None = None) -> None:
        if self._run_id is not None:
            run_id = self._run_id
            self._record(
                lambda: self._store.finish(
                    run_id,
                    status,
                    self.clock(),
                    self.gate,
                    self.signals,
                    self.symptoms,
                    rules=self.rules,
                    error=error,
                )
            )

    @staticmethod
    def _record(write) -> None:
        try:
            write()
        except Exception as e:  # 기록 결함(요약 계산 포함)이 점검을 막거나 원래 예외를 가리지 않게
            logger.warning(f"점검 결과 기록 실패: {type(e).__name__}")


def _rules_summary(signals: list[Signal]) -> str:
    n = Counter(
        "적발"
        if s.verdict == "detected" and CATALOG[s.rule_id].grade == "signal"
        else "개선 권고"
        if s.verdict == "detected"
        else s.verdict
        for s in signals
    )
    text = f"적발 {n['적발']} · 관찰 중 {n['observing']} · 개선 권고 {n['개선 권고']}"
    return text + (f" · 확인하지 못함 {n['unknown']}" if n["unknown"] else "")


def _symptoms_summary(symptoms: list[Symptom]) -> str:
    if not symptoms:
        return "실행한 시나리오 없음"
    parts = [f"{x.scenario} {x.failed_step}단계 실패" for x in symptoms if x.status == "failed"]
    unknown = sum(x.status == "unknown" for x in symptoms)
    if unknown:
        parts.append(f"확인하지 못함 {unknown}")
    return ", ".join(parts) or "모두 통과"


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
    rec = _Recorder(engine, now)
    try:
        runs = CheckRunStore(engine)
        run_id = runs.create(trigger, now)
        excluded = _excluded_rules(engine)
        rec.start(run_id, excluded)
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
        signals = _store_evidence(evidence, snapshot, run_detectors(ctx, excluded))
        rec.signals = signals
        rec.rules = [r for r in CATALOG if r not in excluded]
        rec.phase("rules", lambda: _rules_summary(signals))
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
        rec.symptoms = symptoms
        rec.phase("symptoms", lambda: _symptoms_summary(symptoms))
        gate = decide_gate(signals, symptoms, clean_symptom(user_symptom))
        rec.gate = gate
        rec.phase("gate", lambda: gate.reason)
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
            excluded_rules=excluded,
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
    except BaseException as e:  # SIGTERM(SystemExit)도 행을 running으로 남기지 않는다
        # 끝을 기록하지 못한 조사와 같은 값 (investigate.py)
        interrupted = isinstance(e, SystemExit | KeyboardInterrupt)
        rec.finish("failed", error=INTERRUPTED if interrupted else type(e).__name__)
        if isinstance(e, SQLAlchemyError):
            raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
        raise
    rec.finish("done")
    return outcome


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
