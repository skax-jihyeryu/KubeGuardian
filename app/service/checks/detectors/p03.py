"""P03 이미지 받기 실패 (05-rules §4)."""

from service.checks.detectors._common import (
    Workloads,
    container_statuses,
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

RULE = "P03"
KINDS = ("Pod", "ReplicaSet", "Service")
PULL_FAILURES = {"ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName"}


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data, degraded = grouping_inputs(ctx, KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    signals = []
    for pod in data["Pod"]:
        service = wl.service_of_pod(pod)
        ready_pods = wl.ready_pods_like(pod)
        for cs in container_statuses(pod):
            waiting = (cs.get("state") or {}).get("waiting") or {}
            values = {
                "pod": name_of(pod),
                "container": cs.get("name"),
                "image": cs.get("image"),
                "reason": waiting.get("reason"),
                "message": waiting.get("message"),
                "replicaset": owner(pod, "ReplicaSet"),
                "ready_pods_in_deployment": ready_pods,
            }
            subj = subject(ctx, "Pod", name_of(pod), cs.get("name", ""))
            verdict = hold_verdict(ctx, RULE, subj, waiting.get("reason") in PULL_FAILURES)
            # 같은 Deployment에 Ready Pod가 남아 있으면(이전 버전이 서비스 중) 주의
            severity = primary_severity(ctx, service, fully_down=ready_pods == 0)
            signals.append(
                make_signal(RULE, verdict, subj, service, values, severity, degraded=degraded)
            )
    return signals
