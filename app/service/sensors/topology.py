"""토폴로지: 서비스 호출 관계와 Service → Deployment → ReplicaSet → Pod 계층 (기획서 §7.1).

호출 관계는 Deployment Pod 템플릿의 env와 `envFrom` ConfigMap 값 중 이름이
`topology.url_env_patterns`(glob)에 맞는 값을 URL로 읽어 대상 네임스페이스의 Service·port와 맞춘다.
우선순위는 Kubernetes 규칙대로 뒤의 envFrom이 앞을 덮고 env가 envFrom을 덮는다.
- Secret envFrom과 env valueFrom은 값을 읽지 않는다. 대신 같은 이름의 앞선 값을 지운다
  (덮인 값으로 잘못 판정하지 않기 위해).
- envFrom이 가리키는 ConfigMap·Secret을 수집하지 못했으면 그 호출 서비스와 객체 이름을
  `unchecked`에 넣는다 (envFrom 값은 확인하지 못함). 없는 객체 참조는 C01이 다룬다.
- containers만 본다 (initContainers는 보지 않는다).
- 마스킹으로 포트가 `***`가 된 값은 포트를 모르는 것으로 보고 호스트만 맞춘다.

- declared: 호스트가 네임스페이스의 Service이고 포트가 그 Service에 있음
  (포트를 알 수 없으면 호스트만 본다)
- unresolved: `localhost`·루프백, 네임스페이스에 없는 Service(다른 네임스페이스 포함),
  Service에 없는 포트
- `external_hosts`(glob)에 맞는 호스트는 클러스터 밖 의존이라 관계에서 뺀다
- inferred(에이전트 추정)는 여기서 만들지 않는다
"""

from fnmatch import fnmatchcase
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from core.config import KubeGuardianConfig
from service.model import ClusterSnapshot, Topology, TopologyEdge

Obj = dict[str, Any]

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def build_topology(snapshot: ClusterSnapshot, config: KubeGuardianConfig) -> Topology | None:
    """Service나 Deployment를 수집하지 못했으면 None (주소를 맞춰 볼 수 없음 = 확인하지 못함)."""
    services = snapshot.items("Service")
    deployments = snapshot.items("Deployment")
    if services is None or deployments is None:
        return None
    config_maps, secrets = snapshot.items("ConfigMap"), snapshot.items("Secret")
    sources = _EnvSources(
        _by_name(config_maps, lambda c: c.get("data") or {}),
        _by_name(secrets, lambda s: s.get("keys") or []),
    )
    ports = {s["metadata"]["name"]: _service_ports(s) for s in services}
    edges: list[TopologyEdge] = []
    unchecked: dict[str, set[str]] = {}
    for dep in deployments:
        template = dep["spec"].get("template") or {}
        source = _source_name(dep, template, services)
        for container in (template.get("spec") or {}).get("containers") or []:
            values, unread = _container_env(container, sources)
            if unread:
                unchecked.setdefault(source, set()).update(unread)
            for name, value in values.items():
                if not value or not _matches(name, config.topology.url_env_patterns):
                    continue
                edge = _edge(source, name, value, snapshot.namespace, ports, config.external_hosts)
                if edge is not None:
                    edges.append(edge)
    return Topology(
        services=sorted(ports),
        edges=edges,
        unchecked={src: sorted(names) for src, names in sorted(unchecked.items())},
    )


def _by_name(objs: list[Obj] | None, pick) -> dict | None:
    return None if objs is None else {o["metadata"]["name"]: pick(o) for o in objs}


class _EnvSources(NamedTuple):
    config_maps: dict[str, dict[str, str]] | None  # None = 수집 실패
    secret_keys: dict[str, list[str]] | None


def _container_env(container: Obj, sources: _EnvSources) -> tuple[dict[str, str], set[str]]:
    """컨테이너가 보는 env 이름 → 값과, 수집 실패로 읽지 못한 envFrom 객체 이름."""
    values: dict[str, str] = {}
    unread: set[str] = set()
    for src in container.get("envFrom") or []:
        prefix = src.get("prefix") or ""
        if ref := src.get("configMapRef"):
            if sources.config_maps is None:
                unread.add(ref.get("name", ""))
                values.clear()  # 앞선 값이 덮였는지 알 수 없다
                continue
            for key, value in sources.config_maps.get(ref.get("name", ""), {}).items():
                values[prefix + key] = value
        elif ref := src.get("secretRef"):
            if sources.secret_keys is None:
                unread.add(ref.get("name", ""))
                values.clear()  # 앞선 값이 덮였는지 알 수 없다
                continue
            for key in sources.secret_keys.get(ref.get("name", ""), []):
                values.pop(prefix + key, None)  # Secret 값은 읽지 않는다. 덮인 값은 쓰지 않는다
    for env in container.get("env") or []:
        name = env.get("name", "")
        if "value" in env:
            values[name] = env["value"]
        else:  # valueFrom: 실제 값은 알 수 없지만 envFrom 값을 덮으므로 그 값을 쓰지 않는다
            values.pop(name, None)
    return values, unread


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(name, p) for p in patterns)


def _service_ports(service: Obj) -> set[int]:
    return {p["port"] for p in service["spec"].get("ports") or [] if "port" in p}


def _source_name(dep: Obj, template: Obj, services: list[Obj]) -> str:
    """호출하는 서비스 이름: Pod 템플릿을 선택하는 Service, 없으면 Deployment 이름."""
    labels = (template.get("metadata") or {}).get("labels") or {}
    selecting = sorted(
        s["metadata"]["name"] for s in services if _selects(s["spec"].get("selector"), labels)
    )
    return selecting[0] if selecting else dep["metadata"]["name"]


def _edge(
    source: str,
    env_name: str,
    value: str,
    namespace: str,
    ports: dict[str, set[int]],
    external_hosts: list[str],
) -> TopologyEdge | None:
    value = value.replace(":***", "")  # 마스킹된 포트는 모르는 것으로 본다 (호스트만 맞춘다)
    url = urlsplit(value if "://" in value else f"//{value}")
    host = url.hostname
    if not host:
        return None  # URL이 아닌 값 (경로 등)
    if any(fnmatchcase(host, p) for p in external_hosts):
        return None
    try:
        port = url.port or _DEFAULT_PORTS.get(url.scheme)
        bad_port = False
    except ValueError:  # 숫자가 아니거나 범위 밖인 포트는 맞춰 볼 수 없다
        port, bad_port = None, True
    service = None if bad_port or host in _LOOPBACK else _service_name(host, namespace)
    declared = service is not None and service in ports and (port is None or port in ports[service])
    return TopologyEdge(
        source=source,
        target=service if declared else host,
        port=port,
        env_name=env_name,
        status="declared" if declared else "unresolved",
    )


def _service_name(host: str, namespace: str) -> str | None:
    """`name`, `name.ns`, `name.ns.svc`, `name.ns.svc.cluster.local`이면 name.

    다른 네임스페이스나 그 밖의 호스트는 None.
    """
    name, *rest = host.split(".")
    if not rest:
        return name
    if rest[0] == namespace and rest[1:] in ([], ["svc"], ["svc", "cluster", "local"]):
        return name
    return None


# ── 계층: Service → Deployment → ReplicaSet → Pod ──


class PodPath(NamedTuple):
    deployment: str | None  # 소유 Deployment (없으면 None)
    replicaset: str | None  # 소유 ReplicaSet (없으면 None)
    pod: str


def service_hierarchy(snapshot: ClusterSnapshot) -> dict[str, list[PodPath]] | None:
    """Service마다 selector로 고른 Pod와 그 소유 체인(ownerReferences).

    selector가 없는 Service는 Pod를 고르지 않는다 (05-rules S01 제외).
    Service·Pod·ReplicaSet 중 하나라도 수집하지 못했으면 None.
    """
    services = snapshot.items("Service")
    pods = snapshot.items("Pod")
    replicasets = snapshot.items("ReplicaSet")
    if services is None or pods is None or replicasets is None:
        return None
    rs_owner = {rs["metadata"]["name"]: _controller(rs, "Deployment") for rs in replicasets}
    out: dict[str, list[PodPath]] = {}
    for svc in services:
        selector = svc["spec"].get("selector")
        paths = []
        for pod in pods:
            if not _selects(selector, pod["metadata"].get("labels") or {}):
                continue
            rs = _controller(pod, "ReplicaSet")
            paths.append(PodPath(rs_owner.get(rs) if rs else None, rs, pod["metadata"]["name"]))
        out[svc["metadata"]["name"]] = sorted(
            paths, key=lambda p: (p.deployment or "", p.replicaset or "", p.pod)
        )
    return out


def _selects(selector: dict[str, str] | None, labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _controller(obj: Obj, kind: str) -> str | None:
    for ref in obj["metadata"].get("ownerReferences") or []:
        if ref.get("kind") == kind:
            return ref.get("name")
    return None
