"""H01 readinessProbe 없음·TCP만 (05-rules §4). 개선 권고 (게이트를 열지 않음).

Service가 선택하는 Pod의 컨테이너만 본다. replicas마다 반복하지 않도록 Deployment 단위로 묶는다.
"""

from typing import Any

from service.checks.detectors._common import (
    Workloads,
    inputs,
    make_signal,
    rule_unknown,
    selects,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "H01"
KINDS = ("Service", "Pod", "ReplicaSet")
_HANDLERS = ("httpGet", "exec", "grpc", "tcpSocket")


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    selected = [
        p
        for p in data["Pod"]
        if any(
            selects(s["spec"].get("selector"), p["metadata"].get("labels")) for s in data["Service"]
        )
    ]
    signals: dict[str, Signal] = {}
    for pod in selected:
        for c in (pod.get("spec") or {}).get("containers") or []:
            subj = wl.workload_subject(ctx, pod, c.get("name", ""))
            if subj in signals:
                continue
            probe = c.get("readinessProbe")
            kind = _probe_kind(probe)
            values: dict[str, Any] = {
                "container": c.get("name"),
                "probe": kind,
                "readinessProbe": probe,
            }
            verdict = "detected" if kind in (None, "tcpSocket") else "passed"
            signals[subj] = make_signal(
                RULE, verdict, subj, wl.service_of_pod(pod), values, severity="info"
            )
    return list(signals.values())


def _probe_kind(probe: dict[str, Any] | None) -> str | None:
    if not probe:
        return None
    return next((h for h in _HANDLERS if h in probe), "unknown")
