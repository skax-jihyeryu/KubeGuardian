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
