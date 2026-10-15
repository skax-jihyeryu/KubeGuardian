"""Kubernetes 클라이언트 (읽기 전용, 네임스페이스 제한, 기획서 §7.4·§13).

- 조회(list·로그 읽기)만 제공한다. 쓰기 메서드는 두지 않는다.
- 네임스페이스는 생성할 때 정하고 메서드는 namespace 인자를 받지 않는다
  (에이전트 도구도 `ToolContext.k8s`로 이 클라이언트만 쓴다).
  클러스터 범위 조회(노드)는 `list_nodes`로 따로 둔다.
- 응답은 원본 JSON(camelCase dict)으로 돌려준다. 정규화·마스킹은 `service/sensors/collector`가 한다.
- API 오류·연결 오류는 `K8sCallError`(사유 문자열)로 바꾼다.
  수집기는 이것을 "확인하지 못함"으로 둔다.
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
        self, namespace: str, *, core: Any = None, apps: Any = None, discovery: Any = None
    ):
        self._namespace = namespace
        self._apis = {
            "core": core or client.CoreV1Api(),
            "apps": apps or client.AppsV1Api(),
            "discovery": discovery or client.DiscoveryV1Api(),
        }

    @classmethod
    def from_config(cls, cfg: KubeGuardianConfig) -> "K8sClient":
        _load_kube_config()
        return cls(cfg.target.namespace)

    @property
    def namespace(self) -> str:
        return self._namespace

    def list(self, kind: str) -> Items:
        """네임스페이스 범위 kind의 객체 목록. 대상 네임스페이스에서만 읽는다."""
        if kind not in _LIST_METHODS:
            raise ValueError(f"네임스페이스 범위로 수집하지 않는 kind: {kind}")
        api, method = _LIST_METHODS[kind]
        return self._items(self._call(api, method, self._namespace))

    def list_nodes(self) -> Items:
        """노드 목록 (클러스터 범위). 대상 Pod가 올라간 노드만 고르는 것은 수집기가 한다."""
        return self._items(self._call("core", "list_node"))

    @staticmethod
    def _items(resp: Any) -> Items:
        return json.loads(resp.data).get("items") or []

    def read_log(self, pod: str, container: str, *, previous: bool, tail_lines: int) -> str:
        resp = self._call(
            "core",
            "read_namespaced_pod_log",
            pod,
            self._namespace,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
        )
        return resp.data.decode("utf-8", errors="replace")

    def _call(self, api: str, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._apis[api], method)(
                *args, _preload_content=False, _request_timeout=_TIMEOUT_S, **kwargs
            )
        except ApiException as e:
            raise K8sCallError(f"{e.status} {e.reason}") from e
        except (urllib3.exceptions.HTTPError, OSError) as e:
            raise K8sCallError(f"{type(e).__name__}: {e}") from e
