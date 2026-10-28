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
    severity: str | None  # 1차 심각도

    @property
    def needs_investigation(self) -> bool:
        """이번 점검이 조사를 새로 요구했는가 (새 이슈, 지문 변경)."""
        return self.action in ("opened", "changed")


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
        matched = (
            overlapping[-1] if overlapping else None
        )  # 겹치는 것이 여럿이면 가장 최근을 따른다
        change = _follow(store, matched, fp, gate, trigger, run_id, now)
        if change:
            changes.append(change)
        # 나머지 겹치는 이슈는 이 점검에 연결만 한다 (조사·알림 없음). 대상이 사라지면 따로 해결된다
        for other in overlapping[:-1]:
            if not _gone(other, signals, symptoms):
                action = "continued" if trigger == "schedule" else "seen"
                store.touch(other.id, run_id, now, action)
                changes.append(IncidentChange(other.id, action, other.first_severity))
    for incident in open_incidents:
        if incident is not matched and _gone(incident, signals, symptoms):
            store.resolve(incident.id, run_id, now)
            changes.append(IncidentChange(incident.id, "resolved", incident.first_severity))
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
        return IncidentChange(matched.id, "seen", matched.first_severity)
    detail = _detail(fp)
    if matched is None:
        incident_id = store.open(fp.key, detail, gate.severity, run_id, now)
        return IncidentChange(incident_id, "opened", gate.severity)
    if matched.fingerprint == fp.key:
        store.touch(matched.id, run_id, now, "continued")
        return IncidentChange(matched.id, "continued", matched.first_severity)
    store.change(matched.id, fp.key, detail, gate.severity, run_id, now)
    return IncidentChange(matched.id, "changed", gate.severity)


def _gone(incident: Incident, signals: list[Signal], symptoms: list[Symptom]) -> bool:
    """이번 점검이 그 이슈의 규칙·증상을 모두 확인했고 하나도 남지 않았으면 True.

    확인할 대상이 없는 이슈(사용자 증상만)는 해결을 판단할 수 없어 True가 되지 않는다.
    """
    if not incident.targets and not incident.scenarios:
        return False
    rules = {rule for rule, _ in incident.targets}
    for s in signals:
        if s.rule_id not in rules or s.verdict == "passed":
            continue
        if (s.rule_id, s.subject) in incident.targets:
            return False  # 그 대상이 아직 적발·관찰 중·확인하지 못함
        if s.service and s.service in incident.services:
            return False  # 같은 규칙·서비스의 다른 대상 (재시작으로 Pod 이름이 바뀜)
        if s.verdict == "unknown" and "/" not in s.subject:
            return False  # 규칙 전체를 확인하지 못함 (대상이 네임스페이스)
    ran = {x.scenario: x.status for x in symptoms}
    return all(ran.get(name) == "passed" for name in incident.scenarios)


def _detail(fp: Fingerprint) -> dict:
    return asdict(fp)


def record_investigation(engine: Engine, incident_id: int, result: "AgentResult | None") -> None:
    """조사가 끝난 이슈에 조사 상태와 최종 심각도(1순위 문제의 impact)를 남긴다."""
    store = IncidentStore(engine)
    store.set_investigation(incident_id, result.status if result else "failed")
    report = result.report if result else None
    top = report.top_issue if report else None
    if top:
        store.set_final_severity(incident_id, top.impact)
