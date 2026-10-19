"""감지기 공용 도우미: 입력 확인, 관측 기록 판정, 1차 심각도, 서비스 묶음 (05-rules §0·§3)."""

from typing import Any

from service.checks.detectors.base import DetectorContext, evaluate_hold
from service.checks.detectors.catalog import CATALOG
from service.model import Severity, Signal, Verdict

Obj = dict[str, Any]


# ── 입력과 결과 ──


def inputs(ctx: DetectorContext, *kinds: str) -> dict[str, list[Obj]] | None:
    """규칙이 쓰는 kind 목록. 하나라도 수집하지 못했으면 None (확인하지 못함)."""
    out = {kind: ctx.snapshot.items(kind) for kind in kinds}
    return None if any(v is None for v in out.values()) else out  # type: ignore[return-value]


GROUPING_KINDS = ("Service", "ReplicaSet")


def grouping_inputs(
    ctx: DetectorContext, kinds: tuple[str, ...], grouping: tuple[str, ...] = GROUPING_KINDS
) -> tuple[dict[str, list[Obj] | None] | None, dict[str, str]]:
    """묶음에만 쓰는 kind(grouping)는 수집에 실패해도 판정을 계속한다 (05-rules §0.3).

    나머지 kind가 하나라도 없으면 (None, {}). 아니면 (kind → 목록(실패한 묶음용 kind는 None),
    실패한 묶음용 kind → 사유). 사유가 있으면 신호를 `make_signal(degraded=...)`로 만든다.
    """
    required = [k for k in kinds if k not in grouping]
    if inputs(ctx, *required) is None:
        return None, {}
    out = {kind: ctx.snapshot.items(kind) for kind in kinds}
    degraded = {k: ctx.snapshot.failed.get(k, "수집하지 않음") for k in kinds if out[k] is None}
    return out, degraded


def rule_unknown(
    ctx: DetectorContext, rule_id: str, kinds: tuple[str, ...], message: str = ""
) -> Signal:
    """규칙 전체를 판정하지 못함. 대상은 네임스페이스 하나로 둔다 (러너가 prune을 건너뛴다)."""
    failed = {
        k: ctx.snapshot.failed.get(k, "수집하지 않음")
        for k in kinds
        if ctx.snapshot.items(k) is None
    }
    return Signal(
        rule_id=rule_id,
        verdict="unknown",
        subject=ctx.snapshot.namespace,
        values={"failed_kinds": failed},
        message=message or "필요한 데이터를 수집하지 못함: " + ", ".join(failed),
    )


def make_signal(
    rule_id: str,
    verdict: Verdict,
    subject: str,
    service: str | None,
    values: dict[str, Any],
    severity: Severity | None = None,
    message: str = "",
    degraded: dict[str, str] | None = None,
) -> Signal:
    """severity는 적발일 때만 남긴다.

    degraded(묶음용 kind 수집 실패 → 사유)가 있으면 values의 `failed_kinds`에 남기고,
    서비스 묶음을 확신할 수 없으므로 긴급을 주의로 낮춘다 (05-rules §0.3).
    """
    if degraded:
        values = {**values, "failed_kinds": degraded}
        if severity == "critical":
            severity = "warning"
    return Signal(
        rule_id=rule_id,
        verdict=verdict,
        subject=subject,
        service=service,
        severity=severity if verdict == "detected" else None,
        values=values,
        message=message,
    )


def hold_verdict(
    ctx: DetectorContext,
    rule_id: str,
    subject: str,
    met: bool,
    values: dict[str, Any] | None = None,
) -> Verdict:
    """조건 만족 여부를 관측 기록에 반영하고 판정한다 (05-rules §0.2).

    만족하면 기록을 갱신하고 지속 시간을 채웠는지 본다. 아니면 기록을 지운다.
    지속 시간이 없는 규칙(선언 규칙)은 기록을 쓰지 않는다.
    """
    rule = CATALOG[rule_id]
    if not met:
        if rule.hold is not None:
            ctx.observations.clear(rule_id, subject)
        return "passed"
    if rule.hold is None:
        return "detected"
    obs = ctx.observations.touch(rule_id, subject, ctx.now, values)
    return evaluate_hold(rule, obs.first_seen, ctx.now)


# ── 1차 심각도 (05-rules §3) ──


def core_services(ctx: DetectorContext) -> set[str]:
    return {svc for services in ctx.config.core_paths.values() for svc in services}


def primary_severity(ctx: DetectorContext, service: str | None, fully_down: bool) -> Severity:
    """핵심 경로 서비스가 요청을 하나도 받을 수 없으면 긴급, 나머지는 주의."""
    return "critical" if fully_down and service in core_services(ctx) else "warning"


# ── 객체 도우미 ──


def subject(ctx: DetectorContext, kind: str, name: str, *parts: str) -> str:
    return "/".join((ctx.snapshot.namespace, kind, name, *parts))


def name_of(obj: Obj) -> str:
    return obj["metadata"]["name"]


def selects(selector: dict[str, str] | None, labels: dict[str, str] | None) -> bool:
    """빈 selector는 아무것도 고르지 않는다 (selector 없는 Service, 05-rules S01 제외)."""
    return bool(selector) and all((labels or {}).get(k) == v for k, v in selector.items())


def owner(obj: Obj, kind: str) -> str | None:
    for ref in obj["metadata"].get("ownerReferences") or []:
        if ref.get("kind") == kind:
            return ref.get("name")
    return None


def condition(obj: Obj, type_: str) -> Obj | None:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == type_:
            return c
    return None


def pod_ready(pod: Obj) -> bool:
    c = condition(pod, "Ready")
    return c is not None and c.get("status") == "True"


def container_statuses(pod: Obj) -> list[Obj]:
    status = pod.get("status") or {}
    return [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]


class Workloads:
    """Pod → ReplicaSet → Deployment, 그리고 묶을 서비스 이름.

    서비스 이름은 Pod(또는 Pod 템플릿)를 선택하는 Service 이름(여럿이면 이름순 첫째),
    없으면 Deployment 이름, 그것도 없으면 Pod 이름이다.

    replicasets가 None(수집 실패)이면 Pod의 ReplicaSet 소유자 이름에서 끝의
    `-<pod-template-hash>`를 떼어 Deployment 이름으로 쓴다 (05-rules §0.3).
    """

    def __init__(self, pods: list[Obj], replicasets: list[Obj] | None, services: list[Obj] | None):
        self.pods = pods
        self._rs_unknown = replicasets is None
        self.replicasets = replicasets or []
        self.services = services or []
        self._rs_owner = {name_of(rs): owner(rs, "Deployment") for rs in self.replicasets}

    def deployment_of(self, pod: Obj) -> str | None:
        rs = owner(pod, "ReplicaSet")
        if rs and self._rs_unknown:
            return rs.rsplit("-", 1)[0] if "-" in rs else None
        return self._rs_owner.get(rs) if rs else None

    def service_for_labels(self, labels: dict[str, str] | None, fallback: str) -> str:
        names = sorted(
            name_of(s) for s in self.services if selects(s["spec"].get("selector"), labels)
        )
        return names[0] if names else fallback

    def service_of_pod(self, pod: Obj) -> str:
        return self.service_for_labels(
            pod["metadata"].get("labels"), self.deployment_of(pod) or name_of(pod)
        )

    def service_of_deployment(self, dep: Obj) -> str:
        labels = ((dep["spec"].get("template") or {}).get("metadata") or {}).get("labels")
        return self.service_for_labels(labels, name_of(dep))

    def ready_pods_like(self, pod: Obj) -> int:
        """같은 Deployment(없으면 그 Pod 자신)의 Ready Pod 수."""
        dep = self.deployment_of(pod)
        if dep is None:
            return int(pod_ready(pod))
        return sum(1 for p in self.pods if self.deployment_of(p) == dep and pod_ready(p))

    def workload_subject(self, ctx: DetectorContext, pod: Obj, container: str) -> str:
        """replicas마다 같은 권고를 반복하지 않도록 Deployment 단위로 묶는다."""
        dep = self.deployment_of(pod)
        if dep:
            return subject(ctx, "Deployment", dep, container)
        return subject(ctx, "Pod", name_of(pod), container)
