"""S02 Ready 엔드포인트 없음·일부 (05-rules §4). 15분 지속."""

from service.checks.detectors._common import (
    condition,
    hold_verdict,
    inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    selects,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "S02"
KINDS = ("Service", "EndpointSlice", "Pod")
_SERVICE_NAME = "kubernetes.io/service-name"


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    pods = {name_of(p): p for p in data["Pod"]}
    signals = []
    for svc in data["Service"]:
        name = name_of(svc)
        subj = subject(ctx, "Service", name)
        selector = svc["spec"].get("selector")
        excluded = None
        if not selector:
            excluded = "selector 없음"
        elif not any(selects(selector, p["metadata"].get("labels")) for p in pods.values()):
            excluded = "대상 Pod 0개 (S01)"
        if excluded:
            hold_verdict(ctx, RULE, subj, False)
            signals.append(make_signal(RULE, "passed", subj, name, {"excluded": excluded}))
            continue
        endpoints = [
            ep
            for eps in data["EndpointSlice"]
            if (eps["metadata"].get("labels") or {}).get(_SERVICE_NAME) == name
            for ep in eps.get("endpoints") or []
            if not (ep.get("conditions") or {}).get("terminating")  # 제외: 종료 중
        ]
        ready = [ep for ep in endpoints if (ep.get("conditions") or {}).get("ready")]
        values = {
            "ready": len(ready),
            "total": len(endpoints),
            "not_ready": [_not_ready(ep, pods) for ep in endpoints if ep not in ready],
        }
        met = not ready or len(ready) < len(endpoints)
        verdict = hold_verdict(ctx, RULE, subj, met)
        severity = primary_severity(ctx, name, fully_down=not ready)
        signals.append(make_signal(RULE, verdict, subj, name, values, severity))
    return signals


def _not_ready(ep: dict, pods: dict[str, dict]) -> dict:
    pod_name = (ep.get("targetRef") or {}).get("name")
    pod = pods.get(pod_name or "")
    cond = (condition(pod, "Ready") if pod else None) or {}
    return {
        "pod": pod_name,
        "ready_condition": {k: cond[k] for k in ("status", "reason", "message") if k in cond},
    }
