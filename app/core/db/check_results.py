"""점검 결과 기록 (점검 상세 §6.4, #116).

- 점검이 run id를 얻은 직후 start로 running 행을 만들고, 단계(규칙·증상·게이트)가 끝날 때마다
  phase로 덧붙여 화면이 진행 중에 단계를 하나씩 보여 준다. 끝나면 finish로 판정을 채운다.
- verdicts는 규칙마다 `{rule_id, passed, items}`: passed는 통과 대상 수(`통과 n건`), items는
  통과가 아닌 판정(대상·판정·1차 심각도·이유·값·근거 번호). 이유는 unknown의 사유다.
  규칙 순서는 점검이 돌린 순서이고, 끈 규칙은 없다(excluded_rules에 있다).
- 값과 문구는 표시 전에 마스킹하지 않고 저장 전에 마스킹한다 (화면이 그대로 보여 준다).
- 못 끝난 점검(프로세스가 죽음)은 running으로 남는다. display_status가 잠금 만료가 지나면
  halted로 바꿔 보여 준다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.db.check_lock import CHECK_LOCK_TTL
from core.db.tables import CheckResultRow, require_aware
from core.guard.redact import redact_mapping, redact_text
from service.model import GateResult, Signal, Symptom

DisplayStatus = Literal["running", "done", "failed", "halted"]


@dataclass(frozen=True)
class Phase:
    name: str  # rules / symptoms / gate
    started_at: datetime
    ended_at: datetime
    summary: str


@dataclass(frozen=True)
class CheckResult:
    run_id: int
    status: str  # running / done / failed
    started_at: datetime
    finished_at: datetime | None
    gate_path: str | None
    gate_reason: str | None
    first_severity: str | None
    verdicts: list[dict[str, Any]]
    symptoms: list[dict[str, Any]]
    phases: list[Phase]
    excluded_rules: list[str]
    trace_id: str | None


class CheckResultStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def start(self, run_id: int, now: datetime, excluded_rules: Sequence[str] = ()) -> None:
        row = CheckResultRow(
            run_id=run_id,
            status="running",
            started_at=require_aware(now),
            verdicts=[],
            symptoms=[],
            phases=[],
            excluded_rules=sorted(excluded_rules),
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)

    def phase(
        self, run_id: int, name: str, started_at: datetime, ended_at: datetime, summary: str
    ) -> None:
        entry = {
            "name": name,
            "started_at": require_aware(started_at).isoformat(),
            "ended_at": require_aware(ended_at).isoformat(),
            "summary": redact_text(summary),
        }
        with Session(self._engine) as s, s.begin():
            row = s.get_one(CheckResultRow, run_id)
            row.phases = [*row.phases, entry]  # 새 리스트로 바꿔야 JSON 변경이 저장된다

    def finish(
        self,
        run_id: int,
        status: str,
        now: datetime,
        gate: GateResult | None,
        verdicts: Sequence[Signal],
        symptoms: Sequence[Symptom],
    ) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.get_one(CheckResultRow, run_id)
            row.status = status
            row.finished_at = require_aware(now)
            if gate is not None:
                row.gate_path = gate.path
                row.gate_reason = redact_text(gate.reason)
                row.first_severity = gate.severity
            row.verdicts = _verdicts(verdicts)
            row.symptoms = [redact_mapping(x.model_dump(mode="json")) for x in symptoms]

    def get(self, run_id: int) -> CheckResult | None:
        with Session(self._engine) as s:
            row = s.get(CheckResultRow, run_id)
            return _to_result(row) if row else None

    def for_runs(self, run_ids: Sequence[int]) -> dict[int, CheckResult]:
        """점검 여럿의 결과 (목록용, 쿼리 한 번). 결과가 없는 점검은 빠진다."""
        if not run_ids:
            return {}
        stmt = select(CheckResultRow).where(CheckResultRow.run_id.in_(run_ids))
        with Session(self._engine) as s:
            return {r.run_id: _to_result(r) for r in s.scalars(stmt)}

    @staticmethod
    def display_status(result: CheckResult, now: datetime) -> DisplayStatus:
        """running인데 마지막 진행(시작 또는 마지막 단계 끝)에서 잠금 만료가 지났으면 halted."""
        if result.status != "running":
            return "done" if result.status == "done" else "failed"
        last = max([result.started_at, *(p.ended_at for p in result.phases)])
        return "halted" if now - last > CHECK_LOCK_TTL else "running"


def _verdicts(signals: Sequence[Signal]) -> list[dict[str, Any]]:
    by_rule: dict[str, dict[str, Any]] = {}
    for sig in signals:
        entry = by_rule.setdefault(sig.rule_id, {"rule_id": sig.rule_id, "passed": 0, "items": []})
        if sig.verdict == "passed":
            entry["passed"] += 1
            continue
        entry["items"].append(
            redact_mapping(
                {
                    "subject": sig.subject,
                    "service": sig.service,
                    "verdict": sig.verdict,
                    "severity": sig.severity,
                    "message": sig.message,
                    "values": sig.values,
                    "evidence_id": sig.evidence_id,
                }
            )
        )
    return list(by_rule.values())


def _to_result(row: CheckResultRow) -> CheckResult:
    return CheckResult(
        run_id=row.run_id,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        gate_path=row.gate_path,
        gate_reason=row.gate_reason,
        first_severity=row.first_severity,
        verdicts=row.verdicts or [],
        symptoms=row.symptoms or [],
        phases=[
            Phase(
                name=p["name"],
                started_at=datetime.fromisoformat(p["started_at"]),
                ended_at=datetime.fromisoformat(p["ended_at"]),
                summary=p["summary"],
            )
            for p in row.phases or []
        ],
        excluded_rules=row.excluded_rules or [],
        trace_id=row.trace_id,
    )
