"""P07 OOMKilled (05-rules §4). 지속 시간 없이 종료 시각으로 판정한다."""

from datetime import datetime, timedelta
from typing import Any

from service.checks.detectors._common import (
    Workloads,
    container_statuses,
    inputs,
    make_signal,
    name_of,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "P07"
KINDS = ("Pod", "ReplicaSet", "Service")
RECENT = timedelta(minutes=15)  # 설계값 (05-rules §6, W4 평가에서 조정)


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    signals = []
    for pod in data["Pod"]:
        specs = {c.get("name"): c for c in _containers(pod)}
        for cs in container_statuses(pod):
            term = (cs.get("lastState") or {}).get("terminated") or {}
            finished = _parse(term.get("finishedAt"))
            met = (
                term.get("reason") == "OOMKilled"
                and finished is not None
                and ctx.now - finished <= RECENT
            )
            values = {
                "pod": name_of(pod),
                "container": cs.get("name"),
                "reason": term.get("reason"),
                "finishedAt": term.get("finishedAt"),
                "resources": (specs.get(cs.get("name")) or {}).get("resources") or {},
            }
            subj = subject(ctx, "Pod", name_of(pod), cs.get("name", ""))
            verdict = "detected" if met else "passed"
            signals.append(
                make_signal(RULE, verdict, subj, wl.service_of_pod(pod), values, "warning")
            )
    return signals


def _containers(pod: dict[str, Any]) -> list[dict[str, Any]]:
    spec = pod.get("spec") or {}
    return [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None  # 시간대 없는 값은 비교할 수 없다
