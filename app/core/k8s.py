"""Kubernetes 클라이언트 (읽기 전용, 네임스페이스 제한, 기획서 §7.4·§13).

- 조회(list·로그 읽기)만 제공한다. 쓰기 메서드는 두지 않는다.
- 네임스페이스는 생성할 때 정하고 메서드는 namespace 인자를 받지 않는다
  (에이전트 도구도 `ToolContext.k8s`로 이 클라이언트만 쓴다).
  클러스터 범위 조회(노드)는 `list_nodes`로 따로 둔다.
- 응답은 원본 JSON(camelCase dict)으로 돌려준다. 정규화·마스킹은 `service/sensors/collector`가 한다.
- API 오류·연결 오류는 `K8sCallError`(사유 문자열)로 바꾼다.
  수집기는 이것을 "확인하지 못함"으로 둔다.
- 설정 `exclude_event_api_groups`의 그룹을 대상으로 한 이벤트는 읽을 때 뺀다.
  점검·도구가 모두 이 클라이언트로 읽으므로 한 곳에서 거르면 어디에도 들어가지 않는다 (#82).
"""

import json
from typing import Any

import urllib3
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from core.config import KubeGuardianConfig

# 네임스페이스 범위 kind → (API 이름, 메서드). 클러스터 범위(Node)는 list_nodes로 따로 읽는다
_LIST_METHODS: dict[str, tuple[str, str]] = {
    "Pod": ("core", "list_namespaced_pod"),
    "Service": ("core", "list_namespaced_service"),
    "ConfigMap": ("core", "list_namespaced_config_map"),
    "Secret": ("core", "list_namespaced_secret"),
    "Event": ("core", "list_namespaced_event"),
    "Deployment": ("apps", "list_namespaced_deployment"),
    "ReplicaSet": ("apps", "list_namespaced_replica_set"),
    "EndpointSlice": ("discovery", "list_namespaced_endpoint_slice"),
}
_TIMEOUT_S = 10
_LOG_LIMIT_BYTES = 256 * 1024  # tail 200줄이라도 아주 긴 줄을 막는다

# 클래스 안의 `list` 메서드가 내장 list를 가리므로 반환 타입은 모듈 수준 별칭으로 쓴다
Items = list[dict[str, Any]]


class K8sCallError(Exception):
    """Kubernetes API 호출 실패. 메시지는 사유 한 줄이다 (예: `403 Forbidden`)."""


def _load_kube_config() -> None:
    """클러스터 안이면 ServiceAccount, 아니면 kubeconfig(로컬 minikube)를 쓴다."""
    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()


class K8sClient:
    def __init__(
        self,
        namespace: str,
        *,
        core: Any = None,
        apps: Any = None,
        discovery: Any = None,
        exclude_event_api_groups: tuple[str, ...] = (),
    ):
        self._namespace = namespace
        self._excluded_groups = frozenset(exclude_event_api_groups)
        self._apis = {
            "core": core or client.CoreV1Api(),
            "apps": apps or client.AppsV1Api(),
            "discovery": discovery or client.DiscoveryV1Api(),
        }

    @classmethod
    def from_config(cls, cfg: KubeGuardianConfig) -> "K8sClient":
        _load_kube_config()
        return cls(
            cfg.target.namespace, exclude_event_api_groups=tuple(cfg.exclude_event_api_groups)
        )

    @property
    def namespace(self) -> str:
        return self._namespace

    def list(self, kind: str) -> Items:
        """네임스페이스 범위 kind의 객체 목록. 대상 네임스페이스에서만 읽는다."""
        if kind not in _LIST_METHODS:
            raise ValueError(f"네임스페이스 범위로 수집하지 않는 kind: {kind}")
        api, method = _LIST_METHODS[kind]
        items = _items(self._call(api, method, self._namespace))
        if kind == "Event" and self._excluded_groups:
            items = [e for e in items if _event_group(e) not in self._excluded_groups]
        return items

    def list_nodes(self) -> Items:
        """노드 목록 (클러스터 범위). 대상 Pod가 올라간 노드만 고르는 것은 수집기가 한다."""
        return _items(self._call("core", "list_node"))

    def read_log(self, pod: str, container: str, *, previous: bool, tail_lines: int) -> str:
        body = self._call(
            "core",
            "read_namespaced_pod_log",
            pod,
            self._namespace,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
            limit_bytes=_LOG_LIMIT_BYTES,
        )
        return body.decode("utf-8", errors="replace")

    def _call(self, api: str, method: str, *args: Any, **kwargs: Any) -> bytes:
        """API를 호출하고 응답 본문을 읽는다. 본문을 읽다 끊긴 경우도 K8sCallError다."""
        try:
            resp = getattr(self._apis[api], method)(
                *args, _preload_content=False, _request_timeout=_TIMEOUT_S, **kwargs
            )
            return resp.data
        except ApiException as e:
            raise K8sCallError(f"{e.status} {e.reason}") from e
        except (urllib3.exceptions.HTTPError, OSError) as e:
            raise K8sCallError(f"{type(e).__name__}: {e}") from e


def _event_group(event: dict[str, Any]) -> str:
    """이벤트 대상의 API 그룹. `chaos-mesh.org/v1alpha1` → `chaos-mesh.org`, core(`v1`)는 빈 값."""
    api_version = (event.get("involvedObject") or {}).get("apiVersion") or ""
    return api_version.rpartition("/")[0]


def _items(body: bytes) -> Items:
    try:
        return json.loads(body).get("items") or []
    except (ValueError, AttributeError) as e:
        raise K8sCallError(f"응답을 해석하지 못함: {type(e).__name__}") from e
