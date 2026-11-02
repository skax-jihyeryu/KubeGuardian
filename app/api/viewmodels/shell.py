"""모든 화면에 붙는 GNB·상단 띠 값 (UI 정의서 §6.2)."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine

from api.timefmt import hm
from core.config import KubeGuardianConfig
from core.db import CheckResultStore, CheckRunStore, IncidentStore
from service.notify.notifier import STALL_AFTER

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


def build_shell(engine: Engine, config: KubeGuardianConfig, now: datetime) -> Shell:
    latest = CheckRunStore(engine).latest_run()
    unknown, unknown_run = 0, None
    if latest is not None:
        result = CheckResultStore(engine).get(latest[0])
        if result is not None:
            unknown = unknown_count(result.verdicts, result.symptoms)
            unknown_run = latest[0]
    halted = None
    if latest is not None and now - latest[1] > STALL_AFTER:
        halted = int((now - latest[1]).total_seconds() // 60)
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
    )


def unknown_count(verdicts: list[dict], symptoms: list[dict]) -> int:
    n = sum(1 for rule in verdicts for i in rule.get("items", []) if i.get("verdict") == "unknown")
    return n + sum(1 for s in symptoms if s.get("status") == "unknown")


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


_EVERY_N_MINUTES = re.compile(r"\*/(\d{1,2}) \* \* \* \*")


def _next_time(schedule: str, now: datetime) -> str | None:
    """`*/N * * * *` 형태의 주기만 계산한다. 다른 형태는 표시하지 않는다."""
    m = _EVERY_N_MINUTES.fullmatch(schedule)
    if m is None or int(m.group(1)) == 0 or 60 % int(m.group(1)):
        return None
    step = int(m.group(1))
    base = now.replace(second=0, microsecond=0)
    base -= timedelta(minutes=base.minute % step)
    return hm(base + timedelta(minutes=step))
