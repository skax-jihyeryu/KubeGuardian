import json

import pytest
from kubernetes.client.exceptions import ApiException

from core.config import KubeGuardianConfig
from core.k8s import K8sCallError, K8sClient


class _Resp:
    def __init__(self, body):
        self.data = json.dumps(body).encode() if not isinstance(body, str) else body.encode()


class FakeApi:
    """CoreV1Api·AppsV1Api·DiscoveryV1Api를 흉내 낸다. 호출을 기록한다."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if self.error:
                raise self.error
            if method == "read_namespaced_pod_log":
                return _Resp("line1\nline2\n")
            return _Resp({"items": [{"metadata": {"name": "x"}}]})

        return call


def _client(error=None):
    core, apps, discovery = FakeApi(error), FakeApi(error), FakeApi(error)
    client = K8sClient("dev", core=core, apps=apps, discovery=discovery)
    return client, core, apps, discovery


@pytest.mark.parametrize(
    ("kind", "api", "method"),
    [
        ("Pod", "core", "list_namespaced_pod"),
        ("Service", "core", "list_namespaced_service"),
        ("ConfigMap", "core", "list_namespaced_config_map"),
        ("Secret", "core", "list_namespaced_secret"),
        ("Event", "core", "list_namespaced_event"),
        ("Deployment", "apps", "list_namespaced_deployment"),
        ("ReplicaSet", "apps", "list_namespaced_replica_set"),
        ("EndpointSlice", "discovery", "list_namespaced_endpoint_slice"),
    ],
)
def test_namespaced_kinds_are_listed_only_in_target_namespace(kind, api, method):
    client, core, apps, discovery = _client()
    fake = {"core": core, "apps": apps, "discovery": discovery}[api]

    items = client.list(kind)

    assert items == [{"metadata": {"name": "x"}}]
    name, args, kwargs = fake.calls[0]
    assert name == method
    assert args == ("dev",)
    assert kwargs["_preload_content"] is False  # 원본 JSON(camelCase)을 그대로 받는다


def test_node_is_listed_cluster_wide():
    client, core, _, _ = _client()

    client.list("Node")

    assert core.calls[0][0] == "list_node"


def test_unknown_kind_is_rejected():
    client, *_ = _client()

    with pytest.raises(ValueError, match="Job"):
        client.list("Job")


def test_api_error_becomes_k8s_call_error_with_status_reason():
    client, *_ = _client(error=ApiException(status=403, reason="Forbidden"))

    with pytest.raises(K8sCallError, match="403 Forbidden"):
        client.list("Secret")


def test_connection_error_becomes_k8s_call_error():
    client, *_ = _client(error=ConnectionRefusedError("refused"))

    with pytest.raises(K8sCallError, match="refused"):
        client.list("Pod")


def test_read_log_uses_namespace_and_tail():
    client, core, _, _ = _client()

    text = client.read_log("agent-1", "agent", previous=True, tail_lines=200)

    assert text == "line1\nline2\n"
    name, args, kwargs = core.calls[0]
    assert name == "read_namespaced_pod_log"
    assert args == ("agent-1", "dev")
    assert kwargs["container"] == "agent"
    assert kwargs["previous"] is True
    assert kwargs["tail_lines"] == 200


def test_client_has_no_write_methods():
    public = {m for m in dir(K8sClient) if not m.startswith("_")}

    assert public == {"list", "read_log", "from_config", "namespace"}


def test_from_config_uses_target_namespace(monkeypatch):
    monkeypatch.setattr("core.k8s._load_kube_config", lambda: None)
    config = KubeGuardianConfig.model_validate({"target": {"namespace": "staging"}})

    assert K8sClient.from_config(config).namespace == "staging"
