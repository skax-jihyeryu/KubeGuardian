"""수집기: Kubernetes 객체를 읽어 정규화한 ClusterSnapshot을 만든다 (기획서 §7.4).

- 수집한 kind는 0개여도 빈 목록으로 넣고, 실패한 kind는 `failed`에 사유를 넣는다.
  감지기는 실패한 kind를 "확인하지 못함"으로 판정한다 (05-rules §0).
- 객체는 감지기·도구가 쓰는 필드만 남긴 dict로 정규화한다 (managedFields·annotations 등 제외).
  annotations는 last-applied-configuration에 매니페스트 전체(env 값 포함)가 들어 있어 버린다.
- Secret은 메타데이터와 키 이름만 남긴다. 값은 정규화 단계에서 바로 버린다.
- 그 밖의 문자열(env 값, 이벤트 메시지, ConfigMap 값, 실패 사유)은 `core.guard.redact`로 마스킹한다.
- 로그는 스냅샷에 넣지 않는다. 도구가 `fetch_logs`로 필요할 때 읽는다.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import MASK, redact_mapping, redact_text
from core.k8s import LOG_LIMIT_BYTES, Items, K8sCallError
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
MAX_LOG_TAIL = 200  # 한 번에 돌려주는 줄 수 상한
MAX_LOG_SCAN = 2000  # grep할 때 필터 전에 읽는 줄 수 상한
_LOG_RETRIES = 3  # 바이트 한도에 닿았을 때 줄 수를 줄여 다시 읽는 최대 횟수

Obj = dict[str, Any]


class KubeReader(Protocol):
    """수집기가 쓰는 조회 인터페이스. 구현은 `core.k8s.K8sClient`, 테스트는 가짜 객체."""

    @property
    def namespace(self) -> str: ...

    def list(self, kind: str) -> Items:
        """네임스페이스 범위 kind (대상 네임스페이스 고정)."""
        ...

    def list_nodes(self) -> Items:
        """클러스터 범위 노드 목록."""
        ...

    def read_log(
        self,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
        since_seconds: int | None = None,
        timestamps: bool = False,
    ) -> str: ...


def collect(
    reader: KubeReader,
    now: datetime | None = None,
    kinds: tuple[str, ...] = COLLECTED_KINDS,
) -> ClusterSnapshot:
    """kinds를 주면 그 kind만 읽는다 (도구가 필요한 것만 조회할 때). Node는 Pod와 함께 줘야 한다."""
    snapshot = ClusterSnapshot(namespace=reader.namespace, collected_at=now or datetime.now(UTC))
    for kind in kinds:
        if kind == "Node" and snapshot.items("Pod") is None:
            snapshot.failed[kind] = "Pod 수집에 실패해 대상 노드를 정할 수 없음"
            continue
        try:
            raw = reader.list_nodes() if kind == "Node" else reader.list(kind)
        except K8sCallError as e:
            snapshot.failed[kind] = redact_text(str(e))
            continue
        if kind == "Node":  # 대상 Pod가 올라간 노드만 본다 (05-rules §0)
            hosts = {p["spec"].get("nodeName") for p in snapshot.items("Pod") or []}
            raw = [n for n in raw if _get(n, "metadata", "name") in hosts]
        snapshot.resources[kind] = [_NORMALIZERS[kind](obj) for obj in raw]
    return snapshot


class LogRead(NamedTuple):
    """`fetch_logs` 결과. lines는 마스킹·필터 뒤 끝 tail줄이다."""

    lines: list[str]
    scanned: int  # 필터 전에 읽은 줄 수
    more_before: bool  # 읽은 범위 앞에 줄이 더 있을 수 있음 (잘림)
    match_count: int | None  # grep이 있을 때 필터에 걸린 전체 줄 수
    requested_lines: int  # API에 실제로 요청한 줄 수 (바이트 한도로 다시 읽었으면 줄어든 값)


def parse_grep(grep: str) -> list[str]:
    """`|`(`\\|`도)로 나눈 키워드 목록 (앞뒤 공백 제거). 정규식이 아니다.

    하나도 없으면 ValueError.
    """
    keywords = [k.strip() for k in re.split(r"\\?\|", grep) if k.strip()]
    if not keywords:
        raise ValueError("grep에 키워드가 없음")
    return keywords


def _line_matcher(keyword: str) -> Callable[[str], bool]:
    """소문자 줄이 키워드를 담는지. 숫자 키워드는 앞뒤 경계를 본다.

    `502`가 타임스탬프 `03.502Z`, `id=7f4290` 같은 다른 숫자에 걸리지 않게 한다.
    앞이 숫자·16진 문자·`.`·`,`이거나 `숫자:`(시각)이면, 뒤가 숫자이면 일치하지 않는다.
    `"code":429`처럼 `:` 앞이 숫자가 아니면 일치한다. 정규식은 숫자만 든 키워드로만 만든다.
    """
    if not re.fullmatch(r"[0-9]+", keyword):
        return lambda line: keyword in line
    pattern = re.compile(rf"(?<![0-9a-f.,])(?<!\d:){keyword}(?!\d)")
    return lambda line: pattern.search(line) is not None


def initial_read_lines(tail: int, grep: str | None) -> int:
    """첫 읽기에서 요청하는 줄 수. grep이 없으면 tail보다 하나 더 읽어 앞부분이 있는지 본다."""
    return MAX_LOG_SCAN if grep else tail + 1


def fetch_logs(
    reader: KubeReader,
    pod: str,
    container: str,
    *,
    previous: bool = False,
    tail: int = MAX_LOG_TAIL,
    grep: str | None = None,
    since_seconds: int | None = None,
    timestamps: bool = False,
) -> LogRead:
    """컨테이너 로그를 마스킹해 끝 `tail`줄을 돌려준다. previous=True면 직전 컨테이너의 로그.

    grep이 없으면 끝 tail줄만 읽는다. 있으면 최근 `MAX_LOG_SCAN`줄을 읽어 필터한 뒤
    마지막 tail개를 돌려준다.
    grep은 `|`로 나눈 키워드 중 하나라도 든 줄을 찾는다 (대소문자 무시, 부분 문자열, 정규식 아님).
    마스킹한 뒤의 줄에서 찾는다. 비밀값으로 grep해 값을 추측할 수 없게 하기 위함이다.

    응답이 바이트 한도에 닿으면 최신 줄이 잘렸을 수 있다. 읽는 줄 수를 1/4로 줄여 다시 읽고
    (최대 3번), 그래도 닿으면 COLLECT_FAILED다. 다시 읽었으면 more_before는 True다.
    """
    if not 1 <= tail <= MAX_LOG_TAIL:
        raise ValueError(f"tail은 1~{MAX_LOG_TAIL} 사이여야 한다: {tail}")
    keywords = [k.lower() for k in parse_grep(grep)] if grep else None
    text, retried, requested = _read_within_limit(
        reader, pod, container, previous, initial_read_lines(tail, grep), since_seconds, timestamps
    )
    # 읽은 줄 수와 잘림은 마스킹 전 원문 기준이다 (PEM은 마스킹하면 한 줄로 줄어든다)
    raw_count = len(text.splitlines())
    # 줄로 나누기 전에 전체를 마스킹한다 (PEM 개인 키처럼 여러 줄에 걸친 비밀값)
    read = redact_text(text).splitlines()
    if keywords is None:
        return LogRead(read[-tail:], raw_count, retried or raw_count > tail, None, requested)
    matchers = [_line_matcher(k) for k in keywords]
    matched = [line for line in read if any(m(line.lower()) for m in matchers)]
    more = retried or raw_count >= MAX_LOG_SCAN or len(matched) > tail
    return LogRead(matched[-tail:], raw_count, more, len(matched), requested)


def _read_within_limit(
    reader: KubeReader,
    pod: str,
    container: str,
    previous: bool,
    lines: int,
    since_seconds: int | None,
    timestamps: bool,
) -> tuple[str, bool, int]:
    """바이트 한도 밑이 될 때까지 줄 수를 1/4로 줄여 읽는다. (본문, 다시 읽었는지, 요청한 줄 수)."""
    timestamp_option = {"timestamps": True} if timestamps else {}
    for attempt in range(_LOG_RETRIES + 1):
        try:
            text = reader.read_log(
                pod,
                container,
                previous=previous,
                tail_lines=lines,
                since_seconds=since_seconds,
                **timestamp_option,
            )
        except K8sCallError as e:
            raise ServiceException(ErrorCode.COLLECT_FAILED, redact_text(str(e))) from e
        if len(text.encode("utf-8", "replace")) < LOG_LIMIT_BYTES:
            return text, attempt > 0, lines
        lines = max(1, lines // 4)
    raise ServiceException(
        ErrorCode.COLLECT_FAILED,
        f"로그가 {LOG_LIMIT_BYTES // 1024}KiB 한도를 넘어 줄 수를 줄여도 읽지 못함",
    )


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


_PROBES = ("readinessProbe", "livenessProbe", "startupProbe")


def _container(c: Obj) -> Obj:
    out = _pick(
        c, "name", "image", "restartPolicy", "ports", "env", "envFrom", "resources", *_PROBES
    )
    for key in _PROBES:
        if key in out:
            out[key] = _probe(out[key])
    return out


def _probe(probe: Obj) -> Obj:
    """httpGet 헤더 값은 가린다 (`Authorization: Basic ...`). H01에는 probe 종류·설정만 필요하다."""
    headers = _get(probe, "httpGet", "httpHeaders")
    if not headers:
        return probe
    masked = [{**h, "value": MASK} for h in headers]
    return {**probe, "httpGet": {**probe["httpGet"], "httpHeaders": masked}}


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
    out_status = _pick(status, "phase", "reason", "message", "podIP", "startTime")
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
        "spec": {
            **_pick(obj.get("spec"), "replicas", "selector"),
            # 이전 revision과 이미지·env를 비교하려고 템플릿을 남긴다 (값 마스킹은 Pod와 같은 경로)
            "template": {"spec": _pod_spec(_get(obj, "spec", "template", "spec"))},
        },
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
