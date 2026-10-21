"""점검 실행 흐름 v0: 수집 → 점검 이력·스냅샷 저장 → 규칙 점검 → 게이트 → 근거 저장.

http가 있고 설정에 API 시나리오가 있으면 규칙 점검 뒤에 증상 감지(경로 B)를 돌린다.
조사, 알림은 W2~W3에 이 흐름 뒤에 붙인다.
동시 실행 제한(점검 1건)도 그때 더한다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.config import KubeGuardianConfig
from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore
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
    try:
        runs = CheckRunStore(engine)
        run_id = runs.create(trigger, now)
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
        gate=decide_gate(signals, symptoms, user_symptom),
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
