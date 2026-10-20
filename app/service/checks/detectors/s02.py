"""S02 Ready 엔드포인트 없음·일부 (05-rules §4). 15분 지속."""

from service.checks.detectors._common import (
    condition,
    grouping_inputs,
    hold_verdict,
    make_signal,
    name_of,
    owner,
    primary_severity,
    rule_unknown,
    selects,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal

RULE = "S02"
KINDS = ("Service", "EndpointSlice", "Pod", "ReplicaSet")
_SERVICE_NAME = "kubernetes.io/service-name"
_REVISION = "deployment.kubernetes.io/revision"


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    # ReplicaSet은 롤아웃 제외에만 쓴다. 못 읽으면 제외 없이 판정한다 (05-rules S02 제외, §0.3)
    data, degraded = grouping_inputs(ctx, KINDS, ("ReplicaSet",))
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    pods = {name_of(p): p for p in data["Pod"]}
    revisions = _revisions(data["ReplicaSet"] or [])
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
            values = {"excluded": excluded}
            signals.append(make_signal(RULE, "passed", subj, name, values, degraded=degraded))
            continue
        endpoints = [
            ep
            for eps in data["EndpointSlice"]
            if (eps["metadata"].get("labels") or {}).get(_SERVICE_NAME) == name
            for ep in eps.get("endpoints") or []
            if not (ep.get("conditions") or {}).get("terminating")  # 제외: 종료 중
        ]
        # ready가 비어 있으면 true로 본다 (EndpointSlice API: nil은 true로 해석)
        ready = [ep for ep in endpoints if (ep.get("conditions") or {}).get("ready", True)]
        rollout = _rollout_excluded(endpoints, ready, pods, revisions)  # 제외: 롤아웃 중 새 버전
        endpoints = [ep for ep in endpoints if ep not in rollout]
        values = {
            "ready": len(ready),
            "total": len(endpoints),
            "not_ready": [_not_ready(ep, pods) for ep in endpoints if ep not in ready],
            "rollout_excluded": [_pod_name(ep) for ep in rollout],
        }
        met = not ready or len(ready) < len(endpoints)
        verdict = hold_verdict(ctx, RULE, subj, met)
        severity = primary_severity(ctx, name, fully_down=not ready)
        signals.append(make_signal(RULE, verdict, subj, name, values, severity, degraded=degraded))
    return signals


def _revisions(replicasets: list[dict]) -> dict[str, tuple[str, bool]]:
    """ReplicaSet 이름 → (Deployment 이름, 그 Deployment에서 revision이 가장 큰지).

    revision 값이 없거나 숫자가 아닌 ReplicaSet이 있는 Deployment는 새 버전을 정할 수 없어 뺀다.
    """
    owned = {name_of(rs): owner(rs, "Deployment") for rs in replicasets}
    newest: dict[str, tuple[int, str]] = {}
    unknown = set()
    for rs in replicasets:
        dep = owned[name_of(rs)]
        if not dep:
            continue
        value = str((rs["metadata"].get("annotations") or {}).get(_REVISION, ""))
        if not value.isdecimal():  # int()가 받는 숫자만 (isdigit은 "²"도 참)
            unknown.add(dep)
            continue
        rev = (int(value), name_of(rs))
        newest[dep] = max(newest.get(dep, rev), rev)
    return {
        rs: (dep, newest[dep][1] == rs) for rs, dep in owned.items() if dep and dep not in unknown
    }


def _rollout_excluded(
    endpoints: list[dict],
    ready: list[dict],
    pods: dict[str, dict],
    revisions: dict[str, tuple[str, bool]],
) -> list[dict]:
    """롤아웃 중인 새 버전 Pod의 Ready가 아닌 엔드포인트 (05-rules S02 제외).

    새 버전 = 같은 Deployment에서 revision이 가장 큰 ReplicaSet. 같은 Deployment의
    이전 버전 Pod가 Ready 엔드포인트로 남아 있을 때만 뺀다.
    """

    def revision_of(ep: dict) -> tuple[str, bool] | None:
        pod = pods.get(_pod_name(ep) or "")
        rs = owner(pod, "ReplicaSet") if pod else None
        return revisions.get(rs) if rs else None

    serving_old = {rev[0] for ep in ready if (rev := revision_of(ep)) and not rev[1]}
    return [
        ep
        for ep in endpoints
        if ep not in ready and (rev := revision_of(ep)) and rev[1] and rev[0] in serving_old
    ]


def _pod_name(ep: dict) -> str | None:
    return (ep.get("targetRef") or {}).get("name")


def _not_ready(ep: dict, pods: dict[str, dict]) -> dict:
    pod_name = _pod_name(ep)
    pod = pods.get(pod_name or "")
    cond = (condition(pod, "Ready") if pod else None) or {}
    return {
        "pod": pod_name,
        "ready_condition": {k: cond[k] for k in ("status", "reason", "message") if k in cond},
    }
