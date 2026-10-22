from datetime import UTC, datetime

import pytest

from core.config import KubeGuardianConfig
from service.model import ClusterSnapshot, TopologyEdge
from service.sensors.topology import PodPath, build_topology, service_hierarchy

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
CONFIG = KubeGuardianConfig.model_validate(
    {"target": {"namespace": "dev"}, "external_hosts": ["*.openai.azure.com"]}
)


def _service(name, ports=(8080,), selector=None):
    return {
        "kind": "Service",
        "metadata": {"name": name, "namespace": "dev"},
        "spec": {
            "selector": selector if selector is not None else {"app": name},
            "ports": [{"port": p, "targetPort": p} for p in ports],
        },
    }


def _deployment(name, env, labels=None):
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "dev"},
        "spec": {
            "selector": {"matchLabels": labels or {"app": name}},
            "template": {
                "metadata": {"labels": labels or {"app": name}},
                "spec": {"containers": [{"name": name, "env": env}]},
            },
        },
    }


def _snapshot(services, deployments, pods=(), replicasets=(), failed=None):
    resources = {
        "Service": list(services),
        "Deployment": list(deployments),
        "Pod": list(pods),
        "ReplicaSet": list(replicasets),
    }
    for kind in failed or {}:
        resources.pop(kind)
    return ClusterSnapshot(
        namespace="dev", collected_at=NOW, resources=resources, failed=failed or {}
    )


def _edges(env, services=None):
    snapshot = _snapshot(
        services if services is not None else [_service("agent"), _service("gateway", [80])],
        [_deployment("gateway", env)],
    )
    return build_topology(snapshot, CONFIG).edges


def _env(value, name="AGENT_SERVICE_URL"):
    return [{"name": name, "value": value}]


def test_services_are_listed():
    snapshot = _snapshot([_service("gateway"), _service("agent")], [])

    assert build_topology(snapshot, CONFIG).services == ["agent", "gateway"]


@pytest.mark.parametrize(
    "value",
    [
        "http://agent:8080",
        "http://agent:8080/api/v1",
        "agent:8080",
        "http://agent.dev:8080",
        "http://agent.dev.svc:8080",
        "http://agent.dev.svc.cluster.local:8080",
    ],
)
def test_url_to_service_in_namespace_and_port_is_declared(value):
    assert _edges(_env(value)) == [
        TopologyEdge(
            source="gateway",
            target="agent",
            port=8080,
            env_name="AGENT_SERVICE_URL",
            status="declared",
        )
    ]


def test_default_port_comes_from_scheme():
    edges = _edges(_env("http://gateway"), [_service("gateway", [80])])

    assert edges[0].port == 80
    assert edges[0].status == "declared"


def test_url_without_port_or_known_scheme_is_declared_without_port():
    edges = _edges(_env("redis://agent"))

    assert (edges[0].port, edges[0].status) == (None, "declared")


@pytest.mark.parametrize(
    ("value", "target"),
    [
        ("http://localhost:8080", "localhost"),
        ("http://127.0.0.1:8080", "127.0.0.1"),
        ("http://[::1]:8080", "::1"),
        ("http://ghost:8080", "ghost"),  # 네임스페이스에 없는 Service
        ("http://agent.prod.svc:8080", "agent.prod.svc"),  # 다른 네임스페이스
        ("https://api.example.com", "api.example.com"),  # 허용 목록에 없는 외부 호스트
    ],
)
def test_localhost_and_unknown_hosts_are_unresolved(value, target):
    edges = _edges(_env(value))

    assert len(edges) == 1
    assert edges[0].status == "unresolved"
    assert edges[0].target == target


def test_port_not_on_service_is_unresolved():
    edges = _edges(_env("http://agent:9999"))

    assert (edges[0].target, edges[0].port, edges[0].status) == ("agent", 9999, "unresolved")


def test_invalid_port_is_unresolved():
    edges = _edges(_env("http://agent:80a"))

    assert (edges[0].target, edges[0].port, edges[0].status) == ("agent", None, "unresolved")


def test_external_hosts_are_excluded():
    assert _edges(_env("https://kg.openai.azure.com/", name="AZURE_OPENAI_URL")) == []


def test_only_env_names_matching_patterns_are_parsed():
    env = [
        {"name": "LOG_LEVEL", "value": "http://ghost:1"},
        {"name": "AGENT_URL", "value": "http://agent:8080"},
        {"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        {"name": "EMPTY_URL", "value": ""},
        {"name": "PATH_URL", "value": "/relative/path"},
    ]

    assert [(e.env_name, e.target) for e in _edges(env)] == [("AGENT_URL", "agent")]


def test_url_with_masked_credentials_still_resolves_host():
    edges = _edges(_env("http://user:***@agent:8080"))

    assert (edges[0].target, edges[0].status) == ("agent", "declared")


def test_service_host_with_sensitive_word_survives_collector_redaction():
    """호스트에 token이 들어가도 마스킹이 포트를 가리지 않아 declared로 남는다 (S04 오탐 방지)."""
    from core.guard.redact import redact_mapping

    env = redact_mapping(_env("http://token-service:8080/api"))
    edges = _edges(env, [_service("token-service")])

    assert (edges[0].target, edges[0].port, edges[0].status) == ("token-service", 8080, "declared")


def test_source_falls_back_to_deployment_name_without_service():
    snapshot = _snapshot([_service("agent")], [_deployment("worker", _env("http://agent:8080"))])

    assert build_topology(snapshot, CONFIG).edges[0].source == "worker"


def test_topology_is_unknown_when_services_or_deployments_failed():
    for kind in ("Service", "Deployment"):
        snapshot = _snapshot([_service("agent")], [], failed={kind: "403 Forbidden"})

        assert build_topology(snapshot, CONFIG) is None


def _pod(name, labels, owner=None):
    meta = {"name": name, "namespace": "dev", "labels": labels}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"kind": "Pod", "metadata": meta, "spec": {}, "status": {}}


def _rs(name, deployment):
    return {
        "kind": "ReplicaSet",
        "metadata": {
            "name": name,
            "namespace": "dev",
            "ownerReferences": [{"kind": "Deployment", "name": deployment, "controller": True}],
        },
    }


def test_service_hierarchy_follows_selector_and_owner_references():
    snapshot = _snapshot(
        [_service("agent"), _service("orphan-svc", selector={"app": "nothing"})],
        [],
        pods=[
            _pod("agent-7c9f-a", {"app": "agent"}, ("ReplicaSet", "agent-7c9f")),
            _pod("agent-6b1e-b", {"app": "agent", "x": "y"}, ("ReplicaSet", "agent-6b1e")),
            _pod("agent-debug", {"app": "agent"}),
            _pod("gateway-1", {"app": "gateway"}, ("ReplicaSet", "gateway-1")),
        ],
        replicasets=[_rs("agent-7c9f", "agent"), _rs("agent-6b1e", "agent")],
    )

    assert service_hierarchy(snapshot) == {
        "agent": [
            PodPath(deployment=None, replicaset=None, pod="agent-debug"),
            PodPath(deployment="agent", replicaset="agent-6b1e", pod="agent-6b1e-b"),
            PodPath(deployment="agent", replicaset="agent-7c9f", pod="agent-7c9f-a"),
        ],
        "orphan-svc": [],
    }


def test_service_without_selector_selects_no_pods():
    snapshot = _snapshot([_service("manual", selector={})], [], pods=[_pod("p", {"app": "a"})])

    assert service_hierarchy(snapshot) == {"manual": []}


def test_hierarchy_is_unknown_when_pods_failed():
    snapshot = _snapshot([_service("agent")], [], failed={"Pod": "timeout"})

    assert service_hierarchy(snapshot) is None


def _config_map(name, data):
    return {"kind": "ConfigMap", "metadata": {"name": name, "namespace": "dev"}, "data": data}


def _env_from_snapshot(env_from, config_maps, env=(), failed=None, secrets=None):
    """failed를 주면 config_maps는 무시된다 (그 kind는 수집 실패)."""
    dep = _deployment("gateway", list(env))
    dep["spec"]["template"]["spec"]["containers"][0]["envFrom"] = env_from
    snap = _snapshot([_service("agent"), _service("gateway", [80])], [dep])
    resources = dict(snap.resources)
    if failed:
        return ClusterSnapshot(
            namespace="dev", collected_at=NOW, resources=resources, failed=failed
        )
    resources["ConfigMap"] = config_maps
    if secrets is not None:
        resources["Secret"] = secrets
    return ClusterSnapshot(namespace="dev", collected_at=NOW, resources=resources)


def test_env_from_config_map_urls_become_edges():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}],
        [_config_map("app-config", {"AGENT_SERVICE_URL": "http://localhost:8080", "MODE": "dev"})],
    )

    edges = build_topology(snap, CONFIG).edges

    assert [(e.env_name, e.status, e.target) for e in edges] == [
        ("AGENT_SERVICE_URL", "unresolved", "localhost")
    ]


def test_env_from_prefix_is_applied_to_names():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}, "prefix": "AGENT_"}],
        [_config_map("app-config", {"SERVICE_URL": "http://agent:8080"})],
    )

    edges = build_topology(snap, CONFIG).edges

    assert [(e.env_name, e.status) for e in edges] == [("AGENT_SERVICE_URL", "declared")]


def test_env_value_overrides_env_from_with_same_name():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}],
        [_config_map("app-config", {"AGENT_SERVICE_URL": "http://localhost:8080"})],
        env=[{"name": "AGENT_SERVICE_URL", "value": "http://agent:8080"}],
    )

    edges = build_topology(snap, CONFIG).edges

    assert [(e.env_name, e.status) for e in edges] == [("AGENT_SERVICE_URL", "declared")]


def test_missing_config_map_is_left_to_c01():
    snap = _env_from_snapshot([{"configMapRef": {"name": "gone"}}], [])

    topo = build_topology(snap, CONFIG)

    assert topo.edges == []
    assert topo.unchecked == {}


def test_config_map_collection_failure_marks_source_unchecked():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}], [], failed={"ConfigMap": "403"}
    )

    topo = build_topology(snap, CONFIG)

    assert topo.unchecked == {"gateway": ["app-config"]}


def test_env_value_from_overrides_env_from_and_is_not_guessed():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}],
        [_config_map("app-config", {"AGENT_SERVICE_URL": "http://localhost:8080"})],
        env=[
            {"name": "AGENT_SERVICE_URL", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}
        ],
    )

    assert build_topology(snap, CONFIG).edges == []


def _secret(name, keys):
    return {"kind": "Secret", "metadata": {"name": name, "namespace": "dev"}, "keys": sorted(keys)}


def test_secret_env_from_overriding_config_map_value_is_not_judged():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}, {"secretRef": {"name": "s"}}],
        [_config_map("app-config", {"AGENT_SERVICE_URL": "http://localhost:8080"})],
        secrets=[_secret("s", ["AGENT_SERVICE_URL"])],
    )

    topo = build_topology(snap, CONFIG)

    assert topo.edges == []
    assert topo.unchecked == {}


def test_secret_collection_failure_makes_env_from_unchecked():
    dep = _deployment("gateway", [])
    dep["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "app-config"}},
        {"secretRef": {"name": "s"}},
    ]
    snap = ClusterSnapshot(
        namespace="dev",
        collected_at=NOW,
        resources={
            "Service": [_service("agent"), _service("gateway", [80])],
            "Deployment": [dep],
            "ConfigMap": [
                _config_map("app-config", {"AGENT_SERVICE_URL": "http://localhost:8080"})
            ],
        },
        failed={"Secret": "403"},
    )

    topo = build_topology(snap, CONFIG)

    assert topo.edges == []
    assert topo.unchecked == {"gateway": ["s"]}


def test_later_env_from_source_wins():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "a"}}, {"configMapRef": {"name": "b"}}],
        [
            _config_map("a", {"AGENT_SERVICE_URL": "http://localhost:8080"}),
            _config_map("b", {"AGENT_SERVICE_URL": "http://agent:8080"}),
        ],
    )

    assert [e.status for e in build_topology(snap, CONFIG).edges] == ["declared"]


def test_env_edges_survive_config_map_failure_and_only_env_from_users_are_unchecked():
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}],
        [],
        env=[{"name": "AGENT_SERVICE_URL", "value": "http://localhost:8080"}],
        failed={"ConfigMap": "403"},
    )

    topo = build_topology(snap, CONFIG)

    assert [(e.status, e.target) for e in topo.edges] == [("unresolved", "localhost")]
    assert list(topo.unchecked) == ["gateway"]


@pytest.mark.parametrize("value", ["agent:***", "http://u:***@agent:8080"])
def test_masked_values_resolve_by_host(value):
    snap = _env_from_snapshot(
        [{"configMapRef": {"name": "app-config"}}],
        [_config_map("app-config", {"AGENT_SERVICE_URL": value})],
    )

    assert [e.status for e in build_topology(snap, CONFIG).edges] == ["declared"]


def _sidecar_edges(value, sidecar_ports=(), init_ports=None, own_ports=()):
    """app 컨테이너(env 값)와 같은 Pod의 다른 컨테이너·initContainer를 가진 gateway Deployment."""
    dep = _deployment("gateway", _env(value, "CACHE_URL"))
    spec = dep["spec"]["template"]["spec"]
    spec["containers"][0]["ports"] = [{"containerPort": p} for p in own_ports]
    if sidecar_ports:
        spec["containers"].append(
            {"name": "cache", "ports": [{"containerPort": p} for p in sidecar_ports]}
        )
    if init_ports is not None:
        restart, ports = init_ports
        init = {"name": "init", "ports": [{"containerPort": p} for p in ports]}
        if restart:
            init["restartPolicy"] = restart
        spec["initContainers"] = [init]
    snapshot = _snapshot([_service("gateway", [80])], [dep])
    return build_topology(snapshot, CONFIG).edges


def test_loopback_to_sidecar_port_is_declared():
    (edge,) = _sidecar_edges("http://localhost:6379", sidecar_ports=[6379])

    assert (edge.status, edge.target, edge.port) == ("declared", edge.source, 6379)
    assert edge.via == "sidecar"


def test_regular_edge_has_no_via():
    (edge,) = _edges(_env("http://agent:8080"))

    assert edge.via is None


def test_loopback_to_native_sidecar_init_container_is_declared():
    (edge,) = _sidecar_edges("http://localhost:9000", init_ports=("Always", [9000]))
    assert edge.status == "declared"

    (edge,) = _sidecar_edges("http://localhost:9000", init_ports=(None, [9000]))
    assert edge.status == "unresolved"


def test_loopback_to_own_container_port_is_still_unresolved():
    (edge,) = _sidecar_edges("http://localhost:8080", own_ports=[8080], sidecar_ports=[6379])

    assert edge.status == "unresolved"


def test_loopback_without_port_uses_scheme_default():
    (edge,) = _sidecar_edges("http://localhost", sidecar_ports=[80])
    assert edge.status == "declared"

    (edge,) = _sidecar_edges("http://localhost", sidecar_ports=[8080])
    assert edge.status == "unresolved"


def test_loopback_masked_port_is_unresolved():
    (edge,) = _sidecar_edges("http://localhost:***", sidecar_ports=[80])

    assert edge.status == "unresolved"


@pytest.mark.parametrize("value", ["redis://:***@localhost:6379", "http://user:***@localhost:6379"])
def test_loopback_with_masked_password_is_sidecar(value):
    (edge,) = _sidecar_edges(value, sidecar_ports=[6379])

    assert (edge.status, edge.port) == ("declared", 6379)


def test_loopback_scheme_without_default_port_is_unresolved():
    (edge,) = _sidecar_edges("redis://localhost", sidecar_ports=[6379])

    assert edge.status == "unresolved"
