"""모든 화면에 붙는 GNB·상단 띠 값 (UI 정의서 §6.2)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.config import KubeGuardianConfig
from core.db import CheckLockStore, CheckResultStore, CheckRunStore, IncidentStore
from core.db.tables import CheckRunRow
from core.schedule import next_check, stalled_after
from service.checks.severity import unknown_count
from web.timefmt import hm

CLUSTER_LABEL = "minikube"  # 프로젝트 표시는 지금 고정 값이다

# 심각도 → (CSS 색 키, 화면 이름). 1차 심각도(3값)와 보고서 impact(4값)를 같은 말로 보인다
SEVERITY = {
    "critical": ("crit", "긴급"),
    "warning": ("warn", "주의"),
    "info": ("risk", "잠재 위험"),
}
IMPACT = {
    "core_path_broken": ("crit", "긴급"),
    "partial_users": ("warn", "주의"),
    "latent_risk": ("risk", "잠재 위험"),
    "improvement": ("tip", "개선 권고"),
}


@dataclass(frozen=True)
class Shell:
    project: str
    cluster: str
    namespace: str
    last_time: str | None  # 마지막 점검 시각 (HH:MM)
    next_time: str | None
    halted_minutes: int | None  # 점검이 멈췄으면 몇 분째인가
    open_counts: dict[str, int]  # crit / warn / risk → 열린 이슈 수
    unknown_count: int  # 가장 최근 점검에서 확인하지 못한 항목 수
    unknown_run: int | None
    running: bool = False
    running_run: int | None = None
    cooldown: bool = False
    cooldown_seconds: int = 0


def build_shell(engine: Engine, config: KubeGuardianConfig, now: datetime) -> Shell:
    latest = CheckRunStore(engine).latest_run()
    unknown, unknown_run = 0, None
    if latest is not None:
        result = CheckResultStore(engine).get(latest[0])
        if result is not None:
            unknown = unknown_count(result.verdicts, result.symptoms)
            unknown_run = latest[0]
    halted = None
    if latest is not None and now - latest[1] > stalled_after(config.schedule):
        halted = int((now - latest[1]).total_seconds() // 60)
    current = CheckLockStore(engine).current(now)
    with Session(engine) as session:
        last_ui = session.scalar(
            select(CheckRunRow.started_at)
            .where(CheckRunRow.trigger == "ui")
            .order_by(CheckRunRow.id.desc())
            .limit(1)
        )
    return Shell(
        project=config.project.id,
        namespace=config.target.namespace,
        cluster=CLUSTER_LABEL,
        last_time=hm(latest[1]) if latest else None,
        next_time=_next_time(config.schedule, now),
        halted_minutes=halted,
        open_counts=_open_counts(engine),
        unknown_count=unknown,
        unknown_run=unknown_run if unknown else None,
        running=current is not None,
        running_run=current.run_id if current else None,
        cooldown=last_ui is not None and now - last_ui < timedelta(minutes=1),
        cooldown_seconds=max(0, int(60 - (now - last_ui).total_seconds()) + 1) if last_ui else 0,
    )


def _open_counts(engine: Engine) -> dict[str, int]:
    counts = {"crit": 0, "warn": 0, "risk": 0}
    for inc in IncidentStore(engine).open_incidents():
        key = _incident_key(inc.final_severity, inc.first_severity)
        if key in counts:
            counts[key] += 1
    return counts


def _incident_key(final: str | None, first: str | None) -> str | None:
    if final in IMPACT:
        key = IMPACT[final][0]
        return "risk" if key == "tip" else key  # 개선 권고도 띠에서는 잠재 위험에 센다
    return SEVERITY[first][0] if first in SEVERITY else None


def _next_time(schedule: str, now: datetime) -> str | None:
    value = next_check(schedule, now)
    return hm(value) if value else None
