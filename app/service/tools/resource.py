"""get_topology·get_resource: Kubernetes 리소스 조회 (상세설계 §3.2 1번).

수집기(sensors/collector)로 필요한 kind만 읽어 정규화·마스킹한 뒤, LLM에 줄 핵심 필드만 고른다.
ConfigMap은 키 이름만, Secret은 메타데이터와 키 이름만 돌려준다.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.config import KubeGuardianConfig
from service.sensors.topology import build_topology, service_hierarchy
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
                else {svc: [p._asdict() for p in paths] for svc, paths in hierarchy.items()},
                "unknown": snapshot.failed,
            },
        )


class GetResource(Tool):
    name = "get_resource"
    description = (
        "Kubernetes 리소스 하나(이름) 또는 여러 개(라벨 셀렉터)의 핵심 필드(셀렉터, 포트, "
        "이미지, 환경변수 키, replicas, 상태 조건, 재시작 횟수, 마지막 종료 사유)를 본다. "
        "Service와 Pod 연결, 설정·이미지·재시작 정보를 확인할 때 쓰세요. "
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

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        kinds = ("Pod", "Node") if args.kind == "Node" else (args.kind,)
        objs = require(read(ctx, *kinds), args.kind)
        if args.name:
            objs = [o for o in objs if o["metadata"]["name"] == args.name]
            label = f"name={args.name}"
        elif args.label_selector:
            selector = parse_selector(args.label_selector)
            objs = [o for o in objs if matches(o["metadata"].get("labels"), selector)]
            label = args.label_selector
        else:
            label = "전체"
        items = [_FIELDS[args.kind](o) for o in objs[:MAX_ITEMS]]
        names = ", ".join(i["name"] for i in items[:5]) + (" …" if len(objs) > 5 else "")
        prefix = "cluster" if args.kind == "Node" else ctx.namespace
        # 라벨 키의 `/`(app.kubernetes.io/name)가 subject 구분자와 섞이지 않게 바꾼다
        target = (args.name or args.label_selector or "*").replace("/", "%2F")
        return ToolObservation(
            kind="resource",
            subject=f"{prefix}/{args.kind}/{target}",
            source=f"GET {args.kind} {label}",
            summary=f"{args.kind} {len(objs)}개 ({label})" + (f": {names}" if items else ""),
            data={"kind": args.kind, "count": len(objs), "items": items},
            truncated=len(objs) > MAX_ITEMS,
        )


# ── 핵심 필드 선별 (정규화된 객체에서) ──


def _env(container: Obj) -> list[str]:
    """환경변수는 키만 남긴다. valueFrom 참조는 어디서 오는지 붙인다 (값은 넣지 않는다)."""
    out = []
    for env in container.get("env") or []:
        ref = env.get("valueFrom") or {}
        source = next(iter(ref), None)
        if source and isinstance(ref[source], dict):
            r = ref[source]
            where = "/".join(str(r[k]) for k in ("name", "key", "fieldPath") if k in r)
            out.append(f"{env['name']} ({source} {where})")
        else:
            out.append(env["name"])
    return out


def _containers(spec: Obj) -> list[Obj]:
    out = []
    for c in spec.get("containers") or []:
        item = {"name": c["name"], "image": c.get("image")}
        if c.get("ports"):
            item["ports"] = c["ports"]
        item["env"] = _env(c)
        if c.get("envFrom"):
            item["envFrom"] = c["envFrom"]
        for probe in ("readinessProbe", "livenessProbe"):
            if c.get(probe):
                item[probe] = c[probe]
        out.append(item)
    return out


def _conditions(conditions: list[Obj] | None) -> list[Obj]:
    return [
        {k: clip(c[k]) for k in ("type", "status", "reason", "message") if c.get(k) is not None}
        for c in conditions or []
    ]


def _state(state: Obj | None) -> Obj | None:
    if not state:
        return None
    name, detail = next(iter(state.items()))
    return {"state": name, **{k: detail[k] for k in ("reason", "exitCode") if k in detail}}


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
            item["lastTerminated"] = {k: last[k] for k in ("reason", "exitCode") if k in last}
        statuses.append(item)
    return {
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


def _service(o: Obj) -> Obj:
    spec = o["spec"]
    return {"name": o["metadata"]["name"], **{k: spec[k] for k in spec}}


def _deployment(o: Obj) -> Obj:
    spec, status = o["spec"], dict(o.get("status") or {})
    conditions = _conditions(status.pop("conditions", None))
    out = {
        "name": o["metadata"]["name"],
        "replicas": spec.get("replicas"),
        "selector": spec.get("selector"),
        "containers": _containers((spec.get("template") or {}).get("spec") or {}),
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
