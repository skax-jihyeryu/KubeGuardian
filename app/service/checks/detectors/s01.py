"""S01 selector에 맞는 Pod 없음 (05-rules §4). 선언 비교라 즉시 판정한다."""

from service.checks.detectors._common import (
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

RULE = "S01"
KINDS = ("Service", "Pod")
MAX_SIMILAR = 5


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    signals = []
    for svc in data["Service"]:
        name = name_of(svc)
        subj = subject(ctx, "Service", name)
        selector = svc["spec"].get("selector")
        if not selector:  # 제외: ExternalName, 수동 EndpointSlice
            signals.append(make_signal(RULE, "passed", subj, name, {"excluded": "selector 없음"}))
            continue
        labels = [p["metadata"].get("labels") or {} for p in data["Pod"]]
        matched = sum(1 for lb in labels if selects(selector, lb))
        values = {"selector": selector, "matched_pods": matched}
        if matched == 0:
            # 비교용: selector 키를 하나라도 가진 Pod의 라벨 (오타 확인)
            similar = [lb for lb in labels if set(lb) & set(selector)]
            values["similar_pod_labels"] = _unique(similar)[:MAX_SIMILAR]
        verdict = "detected" if matched == 0 else "passed"
        # 대상 서비스가 요청을 받을 수 없음 → §3 기준
        severity = primary_severity(ctx, name, fully_down=True)
        signals.append(make_signal(RULE, verdict, subj, name, values, severity))
    return signals


def _unique(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
