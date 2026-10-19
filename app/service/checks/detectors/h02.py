"""H02 requests·limits 미설정 (05-rules §4). 개선 권고 (게이트를 열지 않음).

Deployment 단위로 묶는다 (replicas마다 반복하지 않음).
"""

from service.checks.detectors._common import Workloads, grouping_inputs, make_signal, rule_unknown
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "H02"
KINDS = ("Pod", "ReplicaSet", "Service")
_FIELDS = ("requests.cpu", "requests.memory", "limits.cpu", "limits.memory")


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data, degraded = grouping_inputs(ctx, KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    signals: dict[str, Signal] = {}
    for pod in data["Pod"]:
        for c in (pod.get("spec") or {}).get("containers") or []:
            subj = wl.workload_subject(ctx, pod, c.get("name", ""))
            if subj in signals:
                continue
            resources = c.get("resources") or {}
            missing = [
                f
                for f in _FIELDS
                if not (resources.get(f.split(".")[0]) or {}).get(f.split(".")[1])
            ]
            values = {"container": c.get("name"), "resources": resources, "missing": missing}
            verdict = "detected" if missing else "passed"
            signals[subj] = make_signal(
                RULE,
                verdict,
                subj,
                wl.service_of_pod(pod),
                values,
                severity="info",
                degraded=degraded,
            )
    return list(signals.values())
