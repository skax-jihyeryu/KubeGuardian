"""W01 가용 replicas 부족 (05-rules §4).

mixin `KubeDeploymentReplicasMismatch`: `spec.replicas > availableReplicas` 그리고
`updatedReplicas`가 최근 10분 동안 바뀌지 않음, 두 조건이 함께 15분 지속.

관측 기록은 replicas 부족이 이어지는 동안 유지하고, values에 updatedReplicas와
그 마지막 변경 시각을 남긴다. 두 조건이 함께 참이 된 시각은 `변경 시각 + 10분`이다.
"""

from datetime import datetime, timedelta

from service.checks.detectors._common import (
    Workloads,
    grouping_inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, Observation, detector, evaluate_hold
from service.checks.detectors.catalog import CATALOG
from service.model import Signal, Verdict

RULE = "W01"
KINDS = ("Deployment", "Service")
STABLE = timedelta(minutes=10)


def updated_changed_at(obs: Observation | None, updated: int, now: datetime) -> datetime:
    """updatedReplicas의 마지막 변경 시각.

    05-rules W01 "첫 관측": 이전 값을 모르므로 변경 시각을 지금(최초 관측
    시각)으로 둔다. 첫 관측 후 10분이 지나야 "변화 없음"이 되고, 그 뒤 15분 지속이면 적발한다.
    """
    if obs is None or obs.values.get("updatedReplicas") != updated:
        return now
    return datetime.fromisoformat(obs.values["updatedReplicas_changed_at"])


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data, degraded = grouping_inputs(ctx, KINDS, ("Service",))
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads([], None, data["Service"])
    signals = []
    for dep in data["Deployment"]:
        spec_replicas = dep["spec"].get("replicas", 1)
        available = dep["status"].get("availableReplicas", 0)
        updated = dep["status"].get("updatedReplicas", 0)
        subj = subject(ctx, "Deployment", name_of(dep))
        values = {
            "replicas": spec_replicas,
            "availableReplicas": available,
            "updatedReplicas": updated,
        }
        verdict: Verdict = "passed"
        if spec_replicas > available:
            changed = updated_changed_at(ctx.observations.get(RULE, subj), updated, ctx.now)
            ctx.observations.touch(
                RULE,
                subj,
                ctx.now,
                {"updatedReplicas": updated, "updatedReplicas_changed_at": changed.isoformat()},
            )
            stable = ctx.now - changed >= STABLE
            values |= {
                "updatedReplicas_changed_at": changed.isoformat(),
                "updatedReplicas_stable": stable,
            }
            # 제외: updatedReplicas가 바뀌는 중 (롤아웃 진행 중). 기록은 변경 시각 추적용으로 둔다
            if stable:
                verdict = evaluate_hold(CATALOG[RULE], changed + STABLE, ctx.now)
        else:
            ctx.observations.clear(RULE, subj)
        service = wl.service_of_deployment(dep)
        severity = primary_severity(ctx, service, fully_down=available == 0)
        signals.append(
            make_signal(RULE, verdict, subj, service, values, severity, degraded=degraded)
        )
    return signals
