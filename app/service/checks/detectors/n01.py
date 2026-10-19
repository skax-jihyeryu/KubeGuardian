"""N01 노드 NotReady (05-rules §4). 대상 Pod가 올라간 노드만 본다 (수집기가 고른다)."""

from service.checks.detectors._common import (
    condition,
    hold_verdict,
    inputs,
    make_signal,
    name_of,
    rule_unknown,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "N01"
KINDS = ("Node",)


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    signals = []
    for node in data["Node"]:
        ready = condition(node, "Ready") or {}
        unschedulable = bool(node["spec"].get("unschedulable"))
        values = {
            "node": name_of(node),
            "ready": ready.get("status"),
            "reason": ready.get("reason"),
            "message": ready.get("message"),
            "lastTransitionTime": ready.get("lastTransitionTime"),
            "unschedulable": unschedulable,
        }
        subj = f"Node/{name_of(node)}"
        met = ready.get("status") != "True" and not unschedulable
        verdict = hold_verdict(ctx, RULE, subj, met)
        # 단일 노드 환경이라 노드 장애는 전체 서비스 영향 (05-rules N01)
        signals.append(make_signal(RULE, verdict, subj, None, values, severity="critical"))
    return signals
