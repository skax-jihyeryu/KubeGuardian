import pytest

from core.config import KubeGuardianConfig
from core.error.service_exception import ServiceException
from core.k8s import K8sCallError
from service.tools.resource import GetResource, GetTopology
from tests.service.tools.fakes import FakeK8s, deployment, pod, replicaset, service

CONFIG = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}})


def _cluster(**overrides):
    objects = {
        "Service": [service("gateway"), service("agent")],
        "Deployment": [
            deployment(
                "gateway",
                env=[
                    {"name": "AGENT_SERVICE_URL", "value": "http://agent:80"},
                    {"name": "AUTH_SERVICE_URL", "value": "http://localhost:9000"},
                ],
            ),
            deployment("agent"),
        ],
        "ReplicaSet": [replicaset("gateway-rs", "gateway"), replicaset("agent-rs", "agent")],
        "Pod": [pod("gateway-1"), pod("agent-1", app="agent")],
        "ConfigMap": [
            {
                "metadata": {"name": "gw-config", "namespace": "dev"},
                "data": {"MODE": "dev", "DB_URL": "postgres://u:hunter2@db"},
            }
        ],
        "Secret": [
            {
                "metadata": {"name": "gw-secret", "namespace": "dev"},
                "type": "Opaque",
                "data": {"k": "aHVudGVyMg=="},
            }
        ],
    }
    objects.update(overrides)
    return FakeK8s(objects, nodes=[{"metadata": {"name": "minikube"}, "spec": {}}])


# ── get_topology ──


def test_topology_returns_edges_and_service_hierarchy(make_ctx):
    result = GetTopology(CONFIG).run({}, make_ctx(_cluster()))

    assert result.ok
    assert result.data["services"] == ["agent", "gateway"]
    edges = {(e["source"], e["target"], e["status"]) for e in result.data["edges"]}
    assert edges == {("gateway", "agent", "declared"), ("gateway", "localhost", "unresolved")}
    assert result.data["hierarchy"]["gateway"] == [
        {"deployment": "gateway", "replicaset": "gateway-rs", "pod": "gateway-1"}
    ]
    assert result.summary == "서비스 2개, 호출 관계 2개 (unresolved 1개: gateway→localhost)"


def test_topology_reads_only_needed_kinds_via_ctx_k8s(make_ctx):
    k8s = _cluster()

    GetTopology(CONFIG).run({}, make_ctx(k8s))

    assert set(k8s.calls) == {"Service", "Deployment", "Pod", "ReplicaSet"}


def test_topology_fails_when_services_unknown(make_ctx, evidence):
    k8s = _cluster(Service=K8sCallError("403 Forbidden"))

    result = GetTopology(CONFIG).run({}, make_ctx(k8s))

    assert not result.ok
    assert "403 Forbidden" in result.summary
    assert evidence.saved[0].data["error"]


def test_topology_without_pods_keeps_edges_and_marks_hierarchy_unknown(make_ctx):
    k8s = _cluster(Pod=K8sCallError("403 Forbidden"))

    result = GetTopology(CONFIG).run({}, make_ctx(k8s))

    assert result.ok
    assert result.data["hierarchy"] is None
    assert result.data["unknown"] == {"Pod": "403 Forbidden"}


# ── get_resource ──


def test_get_resource_by_name_returns_key_fields_only(make_ctx, evidence):
    result = GetResource().run({"kind": "Pod", "name": "gateway-1"}, make_ctx(_cluster()))

    assert result.ok
    [item] = result.data["items"]
    assert item["name"] == "gateway-1"
    assert item["phase"] == "Running"
    [container] = item["containers"]
    assert container["image"] == "gateway:1.0"
    assert container["env"] == [
        "AGENT_SERVICE_URL",
        "DB_PASSWORD",
        "API_KEY (secretKeyRef gw-secret/k)",
    ]
    assert "hunter2" not in result.model_dump_json()
    assert evidence.saved[0].subject == "dev/Pod/gateway-1"
    assert evidence.saved[0].kind == "resource"
    assert result.summary == "Pod 1개 (name=gateway-1): gateway-1"


def test_get_resource_pod_status_has_restarts_and_last_termination(make_ctx):
    k8s = _cluster(Pod=[pod("gateway-1", restarts=3)])

    result = GetResource().run({"kind": "Pod", "name": "gateway-1"}, make_ctx(k8s))

    [status] = result.data["items"][0]["statuses"]
    assert status["restartCount"] == 3
    assert status["lastTerminated"] == {"reason": "OOMKilled", "exitCode": 137}


def test_get_resource_by_label_selector(make_ctx, evidence):
    k8s = _cluster(Pod=[pod("gateway-1"), pod("gateway-2"), pod("agent-1", app="agent")])

    result = GetResource().run({"kind": "Pod", "label_selector": "app=gateway"}, make_ctx(k8s))

    assert [i["name"] for i in result.data["items"]] == ["gateway-1", "gateway-2"]
    assert evidence.saved[0].subject == "dev/Pod/app=gateway"


def test_selector_with_prefixed_label_key_keeps_subject_shape(make_ctx, evidence):
    p = pod("gateway-1")
    p["metadata"]["labels"]["app.kubernetes.io/name"] = "gateway"

    result = GetResource().run(
        {"kind": "Pod", "label_selector": "app.kubernetes.io/name=gateway"},
        make_ctx(_cluster(Pod=[p])),
    )

    assert result.data["count"] == 1
    assert evidence.saved[0].subject == "dev/Pod/app.kubernetes.io%2Fname=gateway"


def test_get_resource_without_filter_lists_all_with_limit(make_ctx):
    pods = [pod(f"gateway-{i:02d}") for i in range(25)]

    result = GetResource().run({"kind": "Pod"}, make_ctx(_cluster(Pod=pods)))

    assert len(result.data["items"]) == 20
    assert result.data["count"] == 25
    assert result.truncated


def test_get_resource_not_found_is_an_empty_result(make_ctx):
    result = GetResource().run({"kind": "Service", "name": "nope"}, make_ctx(_cluster()))

    assert result.ok
    assert result.data["items"] == []
    assert result.summary == "Service 0개 (name=nope)"


def test_configmap_returns_key_names_only(make_ctx):
    result = GetResource().run({"kind": "ConfigMap", "name": "gw-config"}, make_ctx(_cluster()))

    assert result.data["items"] == [{"name": "gw-config", "keys": ["DB_URL", "MODE"]}]


def test_secret_returns_metadata_and_key_names_only(make_ctx):
    result = GetResource().run({"kind": "Secret", "name": "gw-secret"}, make_ctx(_cluster()))

    assert result.data["items"] == [{"name": "gw-secret", "type": "Opaque", "keys": ["k"]}]


def test_service_and_deployment_key_fields(make_ctx):
    svc = GetResource().run({"kind": "Service", "name": "gateway"}, make_ctx(_cluster()))
    dep = GetResource().run({"kind": "Deployment", "name": "gateway"}, make_ctx(_cluster()))

    assert svc.data["items"][0]["selector"] == {"app": "gateway"}
    assert svc.data["items"][0]["ports"] == [{"name": "http", "port": 80, "targetPort": "http"}]
    item = dep.data["items"][0]
    assert item["replicas"] == 1
    assert item["status"] == {"replicas": 1, "readyReplicas": 1}
    assert item["containers"][0]["env"] == ["AGENT_SERVICE_URL", "AUTH_SERVICE_URL"]


def test_node_subject_is_cluster_scoped(make_ctx, evidence):
    result = GetResource().run({"kind": "Node", "name": "minikube"}, make_ctx(_cluster()))

    assert result.ok
    assert evidence.saved[0].subject == "cluster/Node/minikube"


def test_get_resource_failure_is_saved_and_not_raised(make_ctx):
    k8s = _cluster(Pod=K8sCallError("403 Forbidden"))

    result = GetResource().run({"kind": "Pod"}, make_ctx(k8s))

    assert not result.ok
    assert "403 Forbidden" in result.summary


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "Event"},
        {"kind": "Pod", "name": "a", "label_selector": "app=a"},
        {"kind": "Pod", "label_selector": "app in (a,b)"},
        {"kind": "Pod", "name": "../x"},
        {"kind": "Pod", "namespace": "kube-system"},
    ],
)
def test_get_resource_rejects_bad_input(make_ctx, args):
    with pytest.raises(ServiceException):
        GetResource().run(args, make_ctx(_cluster()))
