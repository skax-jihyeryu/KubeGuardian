"""W02 롤아웃 정체 (05-rules §4). Progressing=False (ProgressDeadlineExceeded)가 15분 지속."""

from typing import Any

from service.checks.detectors._common import (
    Workloads,
    condition,
    grouping_inputs,
    hold_verdict,
    make_signal,
    name_of,
    owner,
    primary_severity,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "W02"
KINDS = ("Deployment", "ReplicaSet", "Service")
_REVISION = "deployment.kubernetes.io/revision"


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data, degraded = grouping_inputs(ctx, KINDS, ("Service",))
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads([], None, data["Service"])
    signals = []
    for dep in data["Deployment"]:
        progressing = condition(dep, "Progressing") or {}
        new, old = _replicasets(name_of(dep), data["ReplicaSet"])
        values = {
            "progressing": {
                k: progressing.get(k) for k in ("status", "reason", "message", "lastTransitionTime")
            },
            "progressDeadlineSeconds": dep["spec"].get("progressDeadlineSeconds"),
            "new_replicaset": new,
            "old_replicasets": old,
        }
        subj = subject(ctx, "Deployment", name_of(dep))
        verdict = hold_verdict(ctx, RULE, subj, progressing.get("status") == "False")
        service = wl.service_of_deployment(dep)
        # 이전 버전 Pod가 Ready로 남아 있으면 주의, 없으면 §3 기준 (Ready Pod가 하나도 없으면 긴급)
        ready = sum(rs["ready"] for rs in [*([new] if new else []), *old])
        severity = primary_severity(ctx, service, fully_down=ready == 0)
        signals.append(
            make_signal(RULE, verdict, subj, service, values, severity, degraded=degraded)
        )
    return signals


def _replicasets(
    deployment: str, replicasets: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Deployment의 ReplicaSet을 새(revision이 가장 큼)·이전으로 나눈다."""
    owned = [rs for rs in replicasets if owner(rs, "Deployment") == deployment]
    owned.sort(key=lambda rs: int((rs["metadata"].get("annotations") or {}).get(_REVISION, 0)))
    summary = [
        {"name": name_of(rs), "ready": (rs.get("status") or {}).get("readyReplicas", 0)}
        for rs in owned
    ]
    return (summary[-1] if summary else None), summary[:-1]
