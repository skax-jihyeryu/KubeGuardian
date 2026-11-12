import json

import pytest

from core.config import KubeGuardianConfig
from core.error.service_exception import ErrorCode, ServiceException
from core.k8s import K8sCallError
from service.tools import build_tools
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
        {"deployment": "gateway", "replicaset": "gateway-rs", "pod": "gateway-1", "ready": True}
    ]
    assert result.summary == "서비스 2개, 호출 관계 2개 (unresolved 1개: gateway→localhost)"


def test_topology_hierarchy_marks_pods_that_are_not_ready(make_ctx):
    cluster = _cluster()
    cluster.objects["Pod"][1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]

    result = GetTopology(CONFIG).run({}, make_ctx(cluster))

    assert [p["ready"] for p in result.data["hierarchy"]["agent"]] == [False]
    assert [p["ready"] for p in result.data["hierarchy"]["gateway"]] == [True]


def test_topology_shows_sidecar_edge_via(make_ctx):
    gateway = deployment("gateway", env=[{"name": "CACHE_URL", "value": "http://localhost:6379"}])
    gateway["spec"]["template"]["spec"]["containers"].append(
        {"name": "cache", "ports": [{"containerPort": 6379}]}
    )
    result = GetTopology(CONFIG).run({}, make_ctx(_cluster(Deployment=[gateway])))

    sidecar = [e for e in result.data["edges"] if e.get("via")]
    assert [(e["source"], e["via"], e["status"]) for e in sidecar] == [
        ("gateway", "sidecar", "declared")
    ]


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


def test_get_resource_replica_set_shows_containers_without_env_values(make_ctx):
    rs = replicaset("agent-rs", "agent")
    rs["spec"]["template"] = {
        "spec": {
            "containers": [
                {
                    "name": "agent",
                    "image": "agent:v1",
                    "env": [
                        {"name": "ADMIN_SERVICE_URL", "value": "http://x"},
                        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
                    ],
                }
            ]
        }
    }
    cluster = _cluster(ReplicaSet=[rs])

    result = GetResource().run({"kind": "ReplicaSet", "name": "agent-rs"}, make_ctx(cluster))

    text = json.dumps(result.data, ensure_ascii=False)
    assert "http://x" not in text
    assert "ADMIN_SERVICE_URL" in text
    assert result.data["items"][0]["containers"][0]["image"] == "agent:v1"


def test_get_resource_pod_has_times_and_resources(make_ctx):
    p = pod("gateway-1", restarts=1)
    p["status"]["startTime"] = "2026-10-20T00:00:30Z"
    p["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-20T00:05:00Z"}
    ]
    status = p["status"]["containerStatuses"][0]
    status["lastState"]["terminated"].update(
        startedAt="2026-10-20T00:01:00Z", finishedAt="2026-10-20T00:04:00Z", message="x"
    )
    p["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "128Mi"},
        "limits": {"memory": "256Mi"},
    }

    result = GetResource().run({"kind": "Pod", "name": "gateway-1"}, make_ctx(_cluster(Pod=[p])))

    item = result.data["items"][0]
    assert item["startTime"] == "2026-10-20T00:00:30Z"
    assert item["conditions"][0]["lastTransitionTime"] == "2026-10-20T00:05:00Z"
    [st] = item["statuses"]
    assert st["state"] == {"state": "running", "startedAt": "2026-10-20T00:01:00Z"}
    assert st["lastTerminated"] == {
        "reason": "OOMKilled",
        "exitCode": 137,
        "startedAt": "2026-10-20T00:01:00Z",
        "finishedAt": "2026-10-20T00:04:00Z",
    }
    assert item["containers"][0]["resources"] == {
        "requests": {"memory": "128Mi"},
        "limits": {"memory": "256Mi"},
    }


def test_get_resource_omits_time_and_resources_when_absent(make_ctx):
    result = GetResource().run({"kind": "Pod", "name": "gateway-1"}, make_ctx(_cluster()))

    item = result.data["items"][0]
    assert "startTime" not in item
    assert "resources" not in item["containers"][0]
    assert item["conditions"] == [{"type": "Ready", "status": "True"}]


def test_get_resource_deployment_and_replicaset_show_resources_and_condition_time(make_ctx):
    dep = deployment("gateway")
    dep["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "1"}}
    dep["status"]["conditions"] = [
        {"type": "Available", "status": "True", "lastTransitionTime": "2026-10-20T00:00:00Z"}
    ]
    rs = replicaset("gateway-rs", "gateway")
    rs["spec"]["template"] = {
        "spec": {"containers": [{"name": "gateway", "resources": {"limits": {"cpu": "1"}}}]}
    }
    cluster = _cluster(Deployment=[dep], ReplicaSet=[rs])

    d = GetResource().run({"kind": "Deployment", "name": "gateway"}, make_ctx(cluster))
    r = GetResource().run({"kind": "ReplicaSet", "name": "gateway-rs"}, make_ctx(cluster))

    assert d.data["items"][0]["containers"][0]["resources"] == {"limits": {"cpu": "1"}}
    assert d.data["items"][0]["conditions"][0]["lastTransitionTime"] == "2026-10-20T00:00:00Z"
    assert r.data["items"][0]["containers"][0]["resources"] == {"limits": {"cpu": "1"}}


@pytest.mark.parametrize(
    ("case", "status", "reason", "observed"),
    [
        ("local", "matched", None, "agent"),
        ("wrong", "mismatch", None, "gateway"),
        ("missing_key", "unknown", "config_map_key_missing", None),
        ("outside", "unsupported", "service_endpoint_out_of_scope", None),
        ("external_name", "unsupported", "service_endpoint_out_of_scope", None),
        ("dynamic", "unsupported", "env_binding", None),
        ("dynamic_value", "unsupported", "service_endpoint_unreadable", None),
        ("uncollected", "unknown", "collection_incomplete", None),
        ("unknown_target", "unknown", "observed_service_missing", None),
        ("unrelated_binding", "unknown", "source_service_selector", None),
        ("sensitive_key", "unsupported", "service_endpoint_unreadable", None),
        ("secret_shadow", "unsupported", "env_binding", None),
        ("optional_secret_shadow", "unsupported", "env_binding", None),
        ("optional_secret_key", "unsupported", "env_binding", None),
        ("lower_precedence_secret", "matched", None, "agent"),
        ("wrong_port", "mismatch", "service_endpoint_unresolved", None),
        ("malformed", "unsupported", "service_endpoint_unreadable", None),
        ("malformed_userinfo", "unsupported", "service_endpoint_unreadable", None),
        ("empty_port", "unsupported", "service_endpoint_unreadable", None),
        ("zero_port", "unsupported", "service_endpoint_unreadable", None),
        ("explicit_non_http_port", "matched", None, "agent"),
        ("unrelated", "unknown", "no_expectation", None),
        ("missing_deployment", "unknown", "deployment_missing", None),
        ("other_namespace", "unknown", "target_mismatch", None),
    ],
)
def test_deployment_config_diagnostics(make_ctx, evidence, caplog, case, status, reason, observed):
    sentinel = "diagnostic-private-value" * 24
    key = "PRIVATE_TOKEN" if case == "sensitive_key" else "AGENT_URL"
    config = KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "staging" if case == "other_namespace" else "dev"},
            "config_baselines": {
                "gateway": {
                    "deployment": "gateway",
                    "container": "gateway",
                    "env": key,
                    "config_map": "gw-config",
                    "key": key,
                    "expected_service": "agent",
                }
            },
        }
    )
    cluster = _cluster()
    binding = {"configMapKeyRef": {"name": "gw-config", "key": key}}
    if case == "dynamic":
        binding = {"fieldRef": {"fieldPath": "status.podIP"}}
    container = cluster.objects["Deployment"][0]["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [{"name": key, "valueFrom": binding}]
    if case in {"secret_shadow", "optional_secret_shadow"}:
        container["env"] = []
        container["envFrom"] = [
            {"configMapRef": {"name": "gw-config"}},
            {
                "secretRef": {
                    "name": "absent" if case == "optional_secret_shadow" else "gw-secret",
                    "optional": case == "optional_secret_shadow",
                }
            },
        ]
    if case == "optional_secret_key":
        container["env"].append(
            {
                "name": key,
                "valueFrom": {"secretKeyRef": {"name": "absent", "key": key, "optional": True}},
            }
        )
    if case == "lower_precedence_secret":
        container["envFrom"] = [{"secretRef": {"name": "gw-secret"}}]
    host = {
        "wrong": "gateway",
        "outside": "agent.staging.svc.cluster.local",
        "unknown_target": "absent",
        "dynamic_value": "$(HOST)",
    }.get(case, "agent")
    url = f"http://private-user:{sentinel}@{host}:80/{sentinel}?q={sentinel}"
    if case == "malformed":
        url = f"http://[{sentinel}"
    elif case == "malformed_userinfo":
        url = f"http://invalid\\userinfo@agent/{sentinel}"
    elif case == "empty_port":
        url = f"http://agent:/{sentinel}"
    elif case == "zero_port":
        url = f"http://agent:0/{sentinel}"
    elif case == "wrong_port":
        url = f"http://agent:81/{sentinel}"
    elif case == "explicit_non_http_port":
        url = f"tcp://agent:80/{sentinel}"
    cluster.objects["ConfigMap"][0]["data"] = {} if case == "missing_key" else {key: url}
    cluster.objects["Secret"][0]["data"] = {"AGENT_URL": sentinel}
    if case == "external_name":
        cluster.objects["Service"][1]["spec"].update(type="ExternalName", externalName=sentinel)
    if case == "missing_deployment":
        cluster.objects["Deployment"].pop(0)
    if case == "uncollected":
        cluster.objects["ConfigMap"] = K8sCallError(sentinel)
    if case == "unrelated_binding":
        cluster.objects["Service"][1]["spec"]["selector"] = {"app": "gateway"}
    tool = next(t for t in build_tools(config) if t.name == "get_resource")

    result = tool.run(
        {"kind": "Deployment", "name": "agent" if case == "unrelated" else "gateway"},
        make_ctx(cluster),
    )

    assert result.ok
    diagnosis = result.data["config_diagnostics"][0]
    assert diagnosis["check"] == "config_map_service_binding"
    assert diagnosis["status"] == status
    assert diagnosis.get("reason") == reason
    assert diagnosis.get("observed_service") == observed
    if case == "local":
        assert diagnosis == {
            "check": "config_map_service_binding",
            "status": "matched",
            "deployment": "gateway",
            "service": "gateway",
            "container": "gateway",
            "env": "AGENT_URL",
            "config_map": "gw-config",
            "key": "AGENT_URL",
            "expected_service": "agent",
            "observed_service": "agent",
        }
    if case == "external_name":
        assert "expected_service" not in diagnosis
    if case == "uncollected":
        assert diagnosis["unavailable_kinds"] == ["ConfigMap"]
    if case == "sensitive_key":
        assert {"env", "key"}.isdisjoint(diagnosis)
        assert key not in result.model_dump_json()
        assert key not in evidence.saved[0].model_dump_json()
        assert key not in caplog.text
    assert evidence.saved[0].data == result.data
    for value in (sentinel, "private-user", "staging.svc.cluster.local"):
        assert value not in result.model_dump_json()
        assert value not in evidence.saved[0].model_dump_json()
        assert value not in caplog.text
    assert "Secret" not in cluster.calls
    if case in {"unrelated", "other_namespace"}:
        assert cluster.calls == ["Deployment"]


def test_named_deployment_rejects_reader_outside_context_namespace(make_ctx, evidence):
    config = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}})
    cluster = _cluster()
    cluster.namespace = "staging"

    with pytest.raises(ServiceException) as error:
        GetResource(config).run({"kind": "Deployment", "name": "gateway"}, make_ctx(cluster))

    assert error.value.error_code == ErrorCode.TOOL_SCOPE_VIOLATION
    assert cluster.calls == []
    assert evidence.saved == []


def test_deployment_config_diagnostics_truncates_expectations(make_ctx):
    expectation = {
        "deployment": "gateway",
        "container": "gateway",
        "env": "AGENT_URL",
        "config_map": "gw-config",
        "key": "AGENT_URL",
        "expected_service": "agent",
    }
    config = KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "config_baselines": {f"source-{i:02}": expectation for i in reversed(range(21))},
        }
    )

    result = GetResource(config).run(
        {"kind": "Deployment", "name": "gateway"}, make_ctx(_cluster())
    )

    assert result.ok and result.truncated
    assert [item["service"] for item in result.data["config_diagnostics"]] == [
        f"source-{i:02}" for i in range(20)
    ]
