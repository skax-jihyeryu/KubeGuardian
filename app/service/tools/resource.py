"""get_topology·get_resource: Kubernetes 리소스 조회 (상세설계 §3.2 1번).

수집기(sensors/collector)로 필요한 kind만 읽어 정규화·마스킹한 뒤, LLM에 줄 핵심 필드만 고른다.
환경변수는 키 이름만, ConfigMap은 키 이름만, Secret은 메타데이터와 키 이름만 돌려준다.
값은 넣지 않는다.
시각(Pod 시작·컨테이너 시작·종료, 조건 변경)과 컨테이너 requests·limits는 있을 때만 넣는다.
"""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.config import KubeGuardianConfig
from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import MASK, is_sensitive_name
from service.config_baseline import evaluate_relation
from service.sensors.topology import build_topology, is_ready, service_hierarchy
from service.tools._common import (
    MAX_ITEMS,
    K8sName,
    LabelSelector,
    clip,
    matches,
    parse_selector,
    read,
    require,
)
from service.tools.base import Tool, ToolContext, ToolObservation

Obj = dict[str, Any]
ResourceKind = Literal[
    "Pod", "Service", "Deployment", "ReplicaSet", "EndpointSlice", "ConfigMap", "Secret", "Node"
]


class GetTopology(Tool):
    name = "get_topology"
    description = (
        "서비스 호출 관계(env의 서비스 주소 → Service)와 Service → Deployment → ReplicaSet → Pod "
        "계층을 본다. 조사를 시작할 때나 어느 서비스가 어디를 호출하는지, 주소가 풀리지 않는 "
        "호출(unresolved)이 있는지 확인할 때 쓰세요."
    )

    class Input(BaseModel):
        pass

    def __init__(self, config: KubeGuardianConfig):
        self._config = config

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        snapshot = read(ctx, "Service", "Deployment", "Pod", "ReplicaSet")
        require(snapshot, "Service")
        require(snapshot, "Deployment")
        topology = build_topology(snapshot, self._config)
        hierarchy = service_hierarchy(snapshot)
        ready = {p["metadata"]["name"]: is_ready(p) for p in snapshot.items("Pod") or []}
        unresolved = [e for e in topology.edges if e.status == "unresolved"]
        summary = f"서비스 {len(topology.services)}개, 호출 관계 {len(topology.edges)}개"
        if unresolved:
            shown = ", ".join(f"{e.source}→{e.target}" for e in unresolved[:5])
            summary += f" (unresolved {len(unresolved)}개: {shown})"
        return ToolObservation(
            kind="resource",
            subject=f"{ctx.namespace}/Topology/{ctx.namespace}",
            source="GET services,deployments,replicasets,pods",
            summary=summary,
            data={
                "services": topology.services,
                "edges": [e.model_dump(exclude_none=True) for e in topology.edges],
                "hierarchy": None
                if hierarchy is None
                else {
                    svc: [{**p._asdict(), "ready": ready.get(p.pod, False)} for p in paths]
                    for svc, paths in hierarchy.items()
                },
                "unknown": snapshot.failed,
            },
        )


class GetResource(Tool):
    name = "get_resource"
    description = (
        "Kubernetes 리소스 하나(이름) 또는 여러 개(라벨 셀렉터)의 핵심 필드(셀렉터, 포트, "
        "이미지, 환경변수 키, requests·limits, replicas, 상태 조건과 변경 시각, 재시작 횟수, "
        "마지막 종료 사유, Pod 시작 시각과 컨테이너 시작·종료 시각)를 본다. "
        "Service와 Pod 연결, 설정·이미지·재시작 정보와 언제 바뀌었는지 확인할 때 쓰세요. "
        "환경변수는 키 이름만, ConfigMap은 키 이름만, Secret은 메타데이터와 키 이름만 보인다. "
        "Deployment를 이름으로 조회하면 설정된 ConfigMap과 로컬 Service 관계의 판정 및 "
        "기준 점검과의 설정 변경 근거도 본다. "
        "이름과 셀렉터를 모두 비우면 그 종류 전체."
    )

    class Input(BaseModel):
        kind: ResourceKind
        name: K8sName | None = None
        label_selector: LabelSelector | None = Field(
            default=None, description="같음 조건만: app=gateway,tier=web"
        )

        @model_validator(mode="after")
        def _one_filter(self):
            if self.name and self.label_selector:
                raise ValueError("name과 label_selector 중 하나만 쓴다")
            return self

    def __init__(
        self,
        config: KubeGuardianConfig | None = None,
        config_comparison: Callable[[str], dict[str, Any]] | None = None,
    ):
        self._config = config
        self._config_comparison = config_comparison

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        kinds = ("Pod", "Node") if args.kind == "Node" else (args.kind,)
        config = self._config if args.kind == "Deployment" and args.name else None
        expectations = []
        if config is not None and ctx.k8s.namespace != ctx.namespace:
            raise ServiceException(ErrorCode.TOOL_SCOPE_VIOLATION, detail=self.name)
        if config is not None and config.target.namespace == ctx.namespace:
            expectations = [
                (service, spec)
                for service, spec in sorted(config.config_baselines.items())
                if spec.deployment == args.name
            ]
        if expectations:
            kinds += ("Service", "ConfigMap")
        snapshot = read(ctx, *kinds)
        objs = require(snapshot, args.kind)
        if args.name:
            objs = [o for o in objs if o["metadata"]["name"] == args.name]
            label = f"name={args.name}"
        elif args.label_selector:
            selector = parse_selector(args.label_selector)
            objs = [o for o in objs if matches(o["metadata"].get("labels"), selector)]
            label = args.label_selector
        else:
            label = "전체"
        items = (
            [_deployment(o, mask_keys=True) for o in objs[:MAX_ITEMS]]
            if expectations
            else [_FIELDS[args.kind](o) for o in objs[:MAX_ITEMS]]
        )
        names = ", ".join(i["name"] for i in items[:5]) + (" …" if len(objs) > 5 else "")
        prefix = "cluster" if args.kind == "Node" else ctx.namespace
        # 라벨 키의 `/`(app.kubernetes.io/name)가 subject 구분자와 섞이지 않게 바꾼다
        target = (args.name or args.label_selector or "*").replace("/", "%2F")
        data = {"kind": args.kind, "count": len(objs), "items": items}
        if config is not None:
            check = {"check": "config_map_service_binding", "deployment": args.name}
            if not expectations:
                reason = (
                    "target_mismatch"
                    if config.target.namespace != ctx.namespace
                    or snapshot.namespace != ctx.namespace
                    else "no_expectation"
                )
                data["config_diagnostics"] = [{**check, "status": "unknown", "reason": reason}]
            else:
                local_services = {
                    obj["metadata"]["name"]
                    for obj in snapshot.items("Service") or []
                    if obj["spec"].get("type") != "ExternalName"
                }
                unavailable = [kind for kind in kinds if snapshot.items(kind) is None]
                diagnostics = []
                for service, spec in expectations[:MAX_ITEMS]:
                    relation = evaluate_relation(
                        config, snapshot, service, spec, require_secret_inventory=False
                    )
                    reason = next(iter(relation.limitations), None)
                    status = relation.result
                    if status == "missing" or reason == "source_service_selector":
                        status = "unknown"
                    diagnostic = {
                        **check,
                        "service": service,
                        "container": spec.container,
                        "config_map": spec.config_map,
                        "status": status,
                    }
                    if spec.expected_service in local_services:
                        diagnostic["expected_service"] = spec.expected_service
                    for field, name in (("env", spec.env), ("key", spec.key)):
                        if not is_sensitive_name(name):
                            diagnostic[field] = name
                    if unavailable:
                        diagnostic["reason"] = "collection_incomplete"
                        diagnostic["unavailable_kinds"] = unavailable
                    elif reason:
                        diagnostic["reason"] = reason
                    if relation.observed_service is not None:
                        diagnostic["observed_service"] = relation.observed_service
                    diagnostics.append(diagnostic)
                data["config_diagnostics"] = diagnostics
                if self._config_comparison is not None and snapshot.namespace == ctx.namespace:
                    data["config_changes"] = [
                        self._config_comparison(service) for service, _ in expectations[:MAX_ITEMS]
                    ]
        return ToolObservation(
            kind="resource",
            subject=f"{prefix}/{args.kind}/{target}",
            source=f"GET {args.kind} {label}",
            summary=f"{args.kind} {len(objs)}개 ({label})" + (f": {names}" if items else ""),
            data=data,
            truncated=max(len(objs), len(expectations)) > MAX_ITEMS,
        )


# ── 핵심 필드 선별 (정규화된 객체에서) ──


def _env(container: Obj, *, mask_keys: bool) -> list[str]:
    """환경변수는 키만 남긴다. valueFrom 참조는 어디서 오는지 붙인다 (값은 넣지 않는다)."""
    out = []
    for env in container.get("env") or []:
        name = env["name"]
        if mask_keys and is_sensitive_name(name):
            name = MASK
        ref = env.get("valueFrom") or {}
        source = next(iter(ref), None)
        if source and isinstance(ref[source], dict):
            r = ref[source]
            where = "/".join(
                MASK if k == "key" and mask_keys and is_sensitive_name(r[k]) else str(r[k])
                for k in ("name", "key", "fieldPath")
                if k in r
            )
            out.append(f"{name} ({source} {where})")
        else:
            out.append(name)
    return out


def _containers(spec: Obj, *, mask_keys: bool = False) -> list[Obj]:
    out = []
    for c in spec.get("containers") or []:
        item = {"name": c["name"], "image": c.get("image")}
        if c.get("ports"):
            item["ports"] = c["ports"]
        item["env"] = _env(c, mask_keys=mask_keys)
        if c.get("envFrom"):
            item["envFrom"] = c["envFrom"]
        if c.get("resources"):
            item["resources"] = c["resources"]
        for probe in ("readinessProbe", "livenessProbe"):
            if c.get(probe):
                item[probe] = c[probe]
        out.append(item)
    return out


_CONDITION_KEYS = ("type", "status", "reason", "message", "lastTransitionTime")


def _conditions(conditions: list[Obj] | None) -> list[Obj]:
    return [
        {k: clip(c[k]) for k in _CONDITION_KEYS if c.get(k) is not None} for c in conditions or []
    ]


_STATE_KEYS = ("reason", "exitCode", "startedAt", "finishedAt")


def _state(state: Obj | None) -> Obj | None:
    if not state:
        return None
    name, detail = next(iter(state.items()))
    return {"state": name, **{k: detail[k] for k in _STATE_KEYS if k in detail}}


def _owner(obj: Obj) -> str | None:
    owners = obj["metadata"].get("ownerReferences") or []
    return f"{owners[0]['kind']}/{owners[0]['name']}" if owners else None


def _pod(o: Obj) -> Obj:
    status = o.get("status") or {}
    statuses = []
    for s in status.get("containerStatuses") or []:
        item = {k: s[k] for k in ("name", "ready", "restartCount") if k in s}
        item["state"] = _state(s.get("state"))
        last = (s.get("lastState") or {}).get("terminated")
        if last:
            item["lastTerminated"] = {k: last[k] for k in _STATE_KEYS if k in last}
        statuses.append(item)
    out = {
        "name": o["metadata"]["name"],
        "labels": o["metadata"].get("labels"),
        "owner": _owner(o),
        "node": o["spec"].get("nodeName"),
        "phase": status.get("phase"),
        "reason": clip(status.get("reason")),
        "podIP": status.get("podIP"),
        "conditions": _conditions(status.get("conditions")),
        "containers": _containers(o["spec"]),
        "statuses": statuses,
    }
    if status.get("startTime"):
        out["startTime"] = status["startTime"]
    return out


def _service(o: Obj) -> Obj:
    spec = o["spec"]
    return {"name": o["metadata"]["name"], **{k: spec[k] for k in spec}}


def _deployment(o: Obj, *, mask_keys: bool = False) -> Obj:
    spec, status = o["spec"], dict(o.get("status") or {})
    conditions = _conditions(status.pop("conditions", None))
    out = {
        "name": o["metadata"]["name"],
        "replicas": spec.get("replicas"),
        "selector": spec.get("selector"),
        "containers": _containers(
            (spec.get("template") or {}).get("spec") or {}, mask_keys=mask_keys
        ),
        "status": status,
    }
    if conditions:
        out["conditions"] = conditions
    return out


_REVISION = "deployment.kubernetes.io/revision"


def _replica_set(o: Obj) -> Obj:
    return {
        "name": o["metadata"]["name"],
        "owner": _owner(o),
        "revision": (o["metadata"].get("annotations") or {}).get(_REVISION),
        "replicas": o["spec"].get("replicas"),
        "containers": _containers((o["spec"].get("template") or {}).get("spec") or {}),
        "status": o.get("status") or {},
    }


def _endpoint_slice(o: Obj) -> Obj:
    return {
        "name": o["metadata"]["name"],
        "service": (o["metadata"].get("labels") or {}).get("kubernetes.io/service-name"),
        "ports": o.get("ports") or [],
        "endpoints": [
            {
                "addresses": e.get("addresses"),
                "ready": (e.get("conditions") or {}).get("ready"),
                "pod": (e.get("targetRef") or {}).get("name"),
            }
            for e in o.get("endpoints") or []
        ],
    }


def _config_map(o: Obj) -> Obj:
    return {
        "name": o["metadata"]["name"],
        "keys": sorted(o.get("data") or {}) + o.get("binaryDataKeys", []),
    }


def _secret(o: Obj) -> Obj:
    return {"name": o["metadata"]["name"], "type": o.get("type"), "keys": o.get("keys", [])}


def _node(o: Obj) -> Obj:
    return {
        "name": o["metadata"]["name"],
        "unschedulable": o["spec"].get("unschedulable", False),
        "conditions": _conditions(o["status"].get("conditions")),
    }


_FIELDS = {
    "Pod": _pod,
    "Service": _service,
    "Deployment": _deployment,
    "ReplicaSet": _replica_set,
    "EndpointSlice": _endpoint_slice,
    "ConfigMap": _config_map,
    "Secret": _secret,
    "Node": _node,
}
