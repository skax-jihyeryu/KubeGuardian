"""P06 반복 재시작 (CrashLoop) (05-rules §4)."""

from typing import Any

from service.checks.detectors._common import (
    Workloads,
    container_statuses,
    hold_verdict,
    inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import ClusterSnapshot, Signal

RULE = "P06"
KINDS = ("Pod", "ReplicaSet", "Service")


def previous_restart_counts(previous: ClusterSnapshot | None) -> dict[tuple[str, str], int]:
    """직전 점검의 (Pod, 컨테이너) → restartCount.

    사용자 확인 필요(임시 기본값): 직전 스냅샷이 없거나 직전에 없던 컨테이너는 비교하지 않고
    CrashLoopBackOff만으로 판정한다. 첫 점검에서 과거 재시작을 증가로 오인하지 않기 위함이다.
    """
    pods = previous.items("Pod") if previous is not None else None
    return {
        (name_of(pod), cs.get("name", "")): cs.get("restartCount", 0)
        for pod in pods or []
        for cs in container_statuses(pod)
    }


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    before = previous_restart_counts(ctx.previous)
    signals = []
    for pod in data["Pod"]:
        service = wl.service_of_pod(pod)
        for cs in container_statuses(pod):
            name = cs.get("name", "")
            count = cs.get("restartCount", 0)
            prev = before.get((name_of(pod), name))
            waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
            increased = prev is not None and count > prev
            # 제외: 지금 Ready이고 restartCount가 늘지 않음 (이미 회복된 과거 재시작)
            met = waiting == "CrashLoopBackOff" or increased
            values = {
                "pod": name_of(pod),
                "container": name,
                "restartCount": count,
                "previous_restartCount": prev,
                "waiting_reason": waiting,
                "last_terminated": _terminated(cs),
            }
            subj = subject(ctx, "Pod", name_of(pod), name)
            verdict = hold_verdict(ctx, RULE, subj, met)
            severity = primary_severity(ctx, service, fully_down=wl.ready_pods_like(pod) == 0)
            signals.append(make_signal(RULE, verdict, subj, service, values, severity))
    return signals


def _terminated(cs: dict[str, Any]) -> dict[str, Any] | None:
    t = (cs.get("lastState") or {}).get("terminated")
    if not t:
        return None
    return {k: t.get(k) for k in ("reason", "exitCode", "finishedAt")}
