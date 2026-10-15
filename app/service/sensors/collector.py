"""수집기: Kubernetes 객체를 읽어 정규화한 ClusterSnapshot을 만든다 (기획서 §7.4).

- 수집한 kind는 0개여도 빈 목록으로 넣고, 실패한 kind는 `failed`에 사유를 넣는다.
  감지기는 실패한 kind를 "확인하지 못함"으로 판정한다 (05-rules §0).
- 객체는 감지기·도구가 쓰는 필드만 남긴 dict로 정규화한다 (managedFields·annotations 등 제외).
  annotations는 last-applied-configuration에 매니페스트 전체(env 값 포함)가 들어 있어 버린다.
- Secret은 메타데이터와 키 이름만 남긴다. 값은 정규화 단계에서 바로 버린다.
- 그 밖의 문자열(env 값, 이벤트 메시지, ConfigMap 값, 실패 사유)은 `core.guard.redact`로 마스킹한다.
- 로그는 스냅샷에 넣지 않는다. 도구가 `fetch_logs`로 필요할 때 읽는다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import redact_mapping, redact_text
from core.k8s import K8sCallError
from service.model import ClusterSnapshot, Evidence

# 수집하는 kind (Kubernetes Kind 이름). Node는 Pod가 올라간 노드를 고르므로 Pod 뒤에 둔다
COLLECTED_KINDS: tuple[str, ...] = (
    "Pod",
    "Node",
    "Service",
    "EndpointSlice",
    "Deployment",
    "ReplicaSet",
    "ConfigMap",
    "Secret",
    "Event",
)
MAX_LOG_TAIL = 200

Obj = dict[str, Any]


class KubeReader(Protocol):
    """수집기가 쓰는 조회 인터페이스. 구현은 `core.k8s.K8sClient`, 테스트는 가짜 객체."""

    @property
    def namespace(self) -> str: ...

    def list(self, kind: str) -> list[Obj]: ...

    def read_log(self, pod: str, container: str, *, previous: bool, tail_lines: int) -> str: ...


def collect(reader: KubeReader, now: datetime | None = None) -> ClusterSnapshot:
    snapshot = ClusterSnapshot(namespace=reader.namespace, collected_at=now or datetime.now(UTC))
    for kind in COLLECTED_KINDS:
        try:
            raw = reader.list(kind)
        except K8sCallError as e:
            snapshot.failed[kind] = redact_text(str(e))
            continue
        if kind == "Node":
            pods = snapshot.items("Pod")
            if pods is None:
                snapshot.failed[kind] = "Pod 수집에 실패해 대상 노드를 정할 수 없음"
                continue
            hosts = {p["spec"].get("nodeName") for p in pods}
            raw = [n for n in raw if _get(n, "metadata", "name") in hosts]
        snapshot.resources[kind] = [_NORMALIZERS[kind](obj) for obj in raw]
    return snapshot


def fetch_logs(
    reader: KubeReader,
    pod: str,
    container: str,
    *,
    previous: bool = False,
    tail: int = MAX_LOG_TAIL,
    grep: str | None = None,
) -> list[str]:
    """컨테이너 로그 끝 `tail`줄을 마스킹해 돌려준다. previous=True면 직전 컨테이너의 로그.

    grep은 마스킹한 뒤의 줄에서 부분 문자열로 찾는다.
    비밀값으로 grep해 값을 추측할 수 없게 하기 위함이다.
    """
    if not 1 <= tail <= MAX_LOG_TAIL:
        raise ValueError(f"tail은 1~{MAX_LOG_TAIL} 사이여야 한다: {tail}")
    try:
        text = reader.read_log(pod, container, previous=previous, tail_lines=tail)
    except K8sCallError as e:
        raise ServiceException(ErrorCode.COLLECT_FAILED, redact_text(str(e))) from e
    lines = [redact_text(line) for line in text.splitlines()]
    if grep:
        lines = [line for line in lines if grep in line]
    return lines


# ── Evidence 변환 ──

_API_PATHS = {
    "Pod": "/api/v1/namespaces/{ns}/pods/{name}",
    "Service": "/api/v1/namespaces/{ns}/services/{name}",
    "ConfigMap": "/api/v1/namespaces/{ns}/configmaps/{name}",
    "Secret": "/api/v1/namespaces/{ns}/secrets/{name}",
    "Event": "/api/v1/namespaces/{ns}/events/{name}",
    "EndpointSlice": "/apis/discovery.k8s.io/v1/namespaces/{ns}/endpointslices/{name}",
    "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    "ReplicaSet": "/apis/apps/v1/namespaces/{ns}/replicasets/{name}",
    "Node": "/api/v1/nodes/{name}",
}


def resource_evidence(snapshot: ClusterSnapshot, obj: Obj) -> Evidence:
    """스냅샷의 정규화된 객체 하나를 Evidence로 만든다. id는 저장소가 발급한다."""
    kind, name = obj["kind"], obj["metadata"]["name"]
    subject = f"{kind}/{name}" if kind == "Node" else f"{snapshot.namespace}/{kind}/{name}"
    path = _API_PATHS[kind].format(ns=snapshot.namespace, name=name)
    return Evidence(
        kind="event" if kind == "Event" else "resource",
        subject=subject,
        collected_at=snapshot.collected_at,
        source=f"GET {path}",
        data=obj,
    )


def log_evidence(
    namespace: str,
    pod: str,
    container: str,
    lines: list[str],
    collected_at: datetime,
    *,
    previous: bool = False,
) -> Evidence:
    """`fetch_logs`로 읽은(마스킹된) 줄을 Evidence로 만든다."""
    query = f"container={container}" + ("&previous=true" if previous else "")
    return Evidence(
        kind="log_excerpt",
        subject=f"{namespace}/Pod/{pod}",
        collected_at=collected_at,
        source=f"GET /api/v1/namespaces/{namespace}/pods/{pod}/log?{query}",
        data={"container": container, "previous": previous, "lines": lines},
    )


# ── 정규화: 감지기(05-rules §4)와 도구가 쓰는 필드만 남긴다 ──


def _get(obj: Obj, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _pick(obj: Obj | None, *keys: str) -> Obj:
    """obj에서 keys 중 값이 있는 것만 남긴다."""
    if not obj:
        return {}
    return {k: obj[k] for k in keys if obj.get(k) is not None}


def _metadata(obj: Obj) -> Obj:
    meta = _pick(
        obj.get("metadata"),
        "name",
        "namespace",
        "uid",
        "labels",
        "creationTimestamp",
        "deletionTimestamp",
        "generation",
    )
    owners = _get(obj, "metadata", "ownerReferences")
    if owners:
        meta["ownerReferences"] = [_pick(o, "kind", "name", "uid", "controller") for o in owners]
    return meta


def _conditions(conditions: list[Obj] | None) -> list[Obj]:
    return [
        _pick(c, "type", "status", "reason", "message", "lastTransitionTime")
        for c in conditions or []
    ]


def _container(c: Obj) -> Obj:
    return _pick(
        c,
        "name",
        "image",
        "ports",
        "env",
        "envFrom",
        "resources",
        "readinessProbe",
        "livenessProbe",
        "startupProbe",
    )


_CONFIG_VOLUME_SOURCES = ("configMap", "secret", "projected")


def _pod_spec(spec: Obj | None) -> Obj:
    spec = spec or {}
    out = _pick(spec, "nodeName")
    for key in ("containers", "initContainers"):
        if spec.get(key):
            out[key] = [_container(c) for c in spec[key]]
    # C01: configMap·secret 참조가 있는 볼륨만 남긴다 (projected는 그 안의 참조만)
    volumes = []
    for v in spec.get("volumes") or []:
        if v.get("projected"):
            sources = [
                s for s in v["projected"].get("sources") or [] if "configMap" in s or "secret" in s
            ]
            if sources:
                volumes.append({"name": v["name"], "projected": {"sources": sources}})
        elif any(k in v for k in ("configMap", "secret")):
            volumes.append(_pick(v, "name", "configMap", "secret"))
    if volumes:
        out["volumes"] = volumes
    return out


def _container_status(s: Obj) -> Obj:
    return _pick(s, "name", "ready", "started", "restartCount", "image", "state", "lastState")


def _pod(obj: Obj) -> Obj:
    status = obj.get("status") or {}
    out_status = _pick(status, "phase", "reason", "message", "podIP")
    out_status["conditions"] = _conditions(status.get("conditions"))
    for key in ("containerStatuses", "initContainerStatuses"):
        if status.get(key):
            out_status[key] = [_container_status(s) for s in status[key]]
    return {"metadata": _metadata(obj), "spec": _pod_spec(obj.get("spec")), "status": out_status}


def _service(obj: Obj) -> Obj:
    return {
        "metadata": _metadata(obj),
        "spec": _pick(obj.get("spec"), "type", "clusterIP", "selector", "ports", "externalName"),
    }


def _endpoint_slice(obj: Obj) -> Obj:
    return {
        "metadata": _metadata(obj),
        "addressType": obj.get("addressType"),
        "endpoints": [
            _pick(e, "addresses", "conditions", "targetRef", "nodeName")
            for e in obj.get("endpoints") or []
        ],
        "ports": obj.get("ports") or [],
    }


def _deployment(obj: Obj) -> Obj:
    spec = obj.get("spec") or {}
    out_spec = _pick(spec, "replicas", "selector", "strategy", "progressDeadlineSeconds", "paused")
    template = spec.get("template") or {}
    out_spec["template"] = {
        "metadata": _pick(template.get("metadata"), "labels"),
        "spec": _pod_spec(template.get("spec")),
    }
    status = obj.get("status") or {}
    out_status = _pick(
        status,
        "replicas",
        "readyReplicas",
        "availableReplicas",
        "updatedReplicas",
        "unavailableReplicas",
        "observedGeneration",
    )
    out_status["conditions"] = _conditions(status.get("conditions"))
    return {"metadata": _metadata(obj), "spec": out_spec, "status": out_status}


_REVISION = "deployment.kubernetes.io/revision"


def _replica_set(obj: Obj) -> Obj:
    meta = _metadata(obj)
    revision = _get(obj, "metadata", "annotations", _REVISION)
    if revision is not None:
        meta["annotations"] = {_REVISION: revision}  # W02: 새·이전 ReplicaSet 구분
    return {
        "metadata": meta,
        "spec": _pick(obj.get("spec"), "replicas", "selector"),
        "status": _pick(obj.get("status"), "replicas", "readyReplicas", "availableReplicas"),
    }


def _config_map(obj: Obj) -> Obj:
    out: Obj = {"metadata": _metadata(obj), "data": obj.get("data") or {}}
    if obj.get("binaryData"):
        out["binaryDataKeys"] = sorted(obj["binaryData"])
    return out


def _secret(obj: Obj) -> Obj:
    keys = set(obj.get("data") or {}) | set(obj.get("stringData") or {})
    return {"metadata": _metadata(obj), "type": obj.get("type"), "keys": sorted(keys)}


def _event(obj: Obj) -> Obj:
    out = _pick(
        obj,
        "reason",
        "message",
        "type",
        "count",
        "firstTimestamp",
        "lastTimestamp",
        "eventTime",
    )
    out["metadata"] = _metadata(obj)
    out["involvedObject"] = _pick(obj.get("involvedObject"), "kind", "name", "namespace", "uid")
    return out


def _node(obj: Obj) -> Obj:
    return {
        "metadata": _metadata(obj),
        "spec": _pick(obj.get("spec"), "unschedulable"),
        "status": {"conditions": _conditions(_get(obj, "status", "conditions"))},
    }


def _with_kind(kind: str, fn: Callable[[Obj], Obj]) -> Callable[[Obj], Obj]:
    def normalize(obj: Obj) -> Obj:
        return redact_mapping({"kind": kind, **fn(obj)})

    return normalize


_NORMALIZERS: dict[str, Callable[[Obj], Obj]] = {
    kind: _with_kind(kind, fn)
    for kind, fn in {
        "Pod": _pod,
        "Node": _node,
        "Service": _service,
        "EndpointSlice": _endpoint_slice,
        "Deployment": _deployment,
        "ReplicaSet": _replica_set,
        "ConfigMap": _config_map,
        "Secret": _secret,
        "Event": _event,
    }.items()
}
