"""이슈 열기·잇기·해결 (기획서 §6.6, UI 정의서 §6.5).

점검 하나의 게이트 결과로 이슈 상태를 정한다. 점검 하나 = 이슈 하나이고, 열린 이슈와 영향 기능이
하나라도 겹치면 같은 이슈로 잇는다 (10/28 사용자 결정).

| 경우 | 행동 |
|---|---|
| 게이트 A·B, 기능이 겹치는 열린 이슈 없음 | opened (조사 대기) |
| 겹치는 열린 이슈, 같은 지문 | continued (조사·알림 생략, 리마인더만은 #27) |
| 겹치는 열린 이슈, 다른 지문 | changed (같은 이슈에서 재조사) |
| 열린 이슈가 이번 점검에서 사라짐 | resolved (그 이슈의 규칙·증상을 모두 확인한 점검일 때만) |

이슈를 열고·잇고·바꾸는 것은 주기 점검(schedule)만 한다. 직접 점검(check·ui·investigate)은
해결만 하고, 겹치는 열린 이슈에는 seen으로 연결만 한다 (UI 정의서 §6.5).
run_check가 점검 잠금 안에서 부른다.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import IncidentStore
from core.db.incidents import Incident
from service.alerts.fingerprint import Fingerprint, compute_fingerprint
from service.model import GateResult, Signal, Symptom, Trigger

if TYPE_CHECKING:
    from service.agent.base import AgentResult

Action = Literal["opened", "continued", "changed", "seen", "resolved"]


@dataclass(frozen=True)
class IncidentChange:
    incident_id: int
    action: Action
    needs_investigation: bool  # opened·changed만 True (조사 대기)
    severity: str | None  # 1차 심각도


def track(
    engine: Engine,
    config: KubeGuardianConfig,
    *,
    run_id: int,
    trigger: Trigger,
    now: datetime,
    gate: GateResult,
    signals: list[Signal],
    symptoms: list[Symptom],
) -> list[IncidentChange]:
    store = IncidentStore(engine)
    open_incidents = store.open_incidents()
    changes: list[IncidentChange] = []
    matched: Incident | None = None
    if gate.path in ("A", "B"):
        fp = compute_fingerprint(gate, config)
        overlapping = [i for i in open_incidents if set(i.features) & set(fp.features)]
        matched = overlapping[-1] if overlapping else None  # 겹치는 것이 여럿이면 가장 최근
        change = _follow(store, matched, fp, gate, trigger, run_id, now)
        if change:
            changes.append(change)
    for incident in open_incidents:
        if incident is not matched and _gone(incident, signals, symptoms):
            store.resolve(incident.id, run_id, now)
            changes.append(IncidentChange(incident.id, "resolved", False, incident.first_severity))
    return changes


def _follow(
    store: IncidentStore,
    matched: Incident | None,
    fp: Fingerprint,
    gate: GateResult,
    trigger: Trigger,
    run_id: int,
    now: datetime,
) -> IncidentChange | None:
    if trigger != "schedule":
        if matched is None:
            return None  # 직접 점검은 이슈를 새로 열지 않는다
        store.touch(matched.id, run_id, now, "seen")
        return IncidentChange(matched.id, "seen", False, matched.first_severity)
    detail = _detail(fp)
    if matched is None:
        incident_id = store.open(fp.key, detail, gate.severity, run_id, now)
        return IncidentChange(incident_id, "opened", True, gate.severity)
    if matched.fingerprint == fp.key:
        store.touch(matched.id, run_id, now, "continued")
        return IncidentChange(matched.id, "continued", False, matched.first_severity)
    store.change(matched.id, fp.key, detail, gate.severity, run_id, now)
    return IncidentChange(matched.id, "changed", True, gate.severity)


def _gone(incident: Incident, signals: list[Signal], symptoms: list[Symptom]) -> bool:
    """이번 점검이 그 이슈의 규칙·증상을 모두 확인했고 하나도 남지 않았으면 True."""
    rules = {rule for rule, _ in incident.targets}
    for s in signals:
        if s.rule_id not in rules or s.verdict == "passed":
            continue
        if (s.rule_id, s.subject) in incident.targets:
            return False  # 그 대상이 아직 적발·관찰 중·확인하지 못함
        if s.verdict == "unknown" and "/" not in s.subject:
            return False  # 규칙 전체를 확인하지 못함 (대상이 네임스페이스)
    ran = {x.scenario: x.status for x in symptoms}
    return all(ran.get(name) == "passed" for name in incident.scenarios)


def _detail(fp: Fingerprint) -> dict:
    d = asdict(fp)
    d["targets"] = [list(t) for t in fp.targets]
    return d


def record_investigation(engine: Engine, incident_id: int, result: "AgentResult | None") -> None:
    """조사가 끝난 이슈에 조사 상태와 최종 심각도(1순위 문제의 impact)를 남긴다."""
    store = IncidentStore(engine)
    store.set_investigation(incident_id, result.status if result else "failed")
    report = result.report if result else None
    if report and report.issues:
        top = min(report.issues, key=lambda i: i.rank)
        store.set_final_severity(incident_id, top.impact)
