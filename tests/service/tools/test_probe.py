import pytest

import service.tools.probe as probe_module
from core.error.service_exception import ServiceException
from core.http import HttpCallError
from service.tools.probe import Probe
from tests.service.tools.fakes import FakeHttp, FakeK8s, pod, service


def _k8s():
    return FakeK8s(
        {
            "Service": [service("gateway", port=80, target_port="http")],
            "Pod": [
                pod("gateway-1", ip="10.0.0.5"),
                pod("gateway-2", ip="10.0.0.6"),
                pod("agent-1", app="agent", ip="10.0.0.7", port=9000),
            ],
        }
    )


def test_probe_via_service_repeats_and_counts(make_ctx, evidence):
    http = FakeHttp({("gateway", "/health"): [200, 500, 200]})

    result = Probe().run(
        {"target": "gateway", "path": "/health", "times": 3}, make_ctx(_k8s(), http)
    )

    assert result.ok
    assert http.calls == [("GET", "gateway", 80, "/health")] * 3
    assert [c["status"] for c in result.data["calls"]] == [200, 500, 200]
    assert result.data["calls"][0]["latency_ms"] == 12.0
    assert (result.data["ok"], result.data["fail"]) == (2, 1)
    saved = evidence.saved[0]
    assert saved.kind == "probe_result"
    assert saved.subject == "dev/Service/gateway"
    assert result.summary == "Service gateway /health 서비스 경유 3회: 성공 2·실패 1, 평균 12ms"


def test_probe_via_pod_calls_each_selected_pod_on_target_port(make_ctx):
    http = FakeHttp({("10.0.0.6", "/health"): 503})

    result = Probe().run(
        {"target": "gateway", "path": "/health", "via": "pod", "times": 2}, make_ctx(_k8s(), http)
    )

    assert sorted(set(http.calls)) == [
        ("GET", "10.0.0.5", 8080, "/health"),
        ("GET", "10.0.0.6", 8080, "/health"),
    ]
    assert len(http.calls) == 4
    assert result.data["pods"] == {
        "gateway-1": {"ok": 2, "fail": 0},
        "gateway-2": {"ok": 0, "fail": 2},
    }
    assert "실패 Pod: gateway-2" in result.summary


def test_probe_pod_target_uses_container_port(make_ctx, evidence):
    http = FakeHttp()

    result = Probe().run(
        {"target": "agent-1", "path": "/health", "via": "pod", "times": 1}, make_ctx(_k8s(), http)
    )

    assert http.calls == [("GET", "10.0.0.7", 9000, "/health")]
    assert result.data["pods"] == {"agent-1": {"ok": 1, "fail": 0}}
    assert evidence.saved[0].subject == "dev/Pod/agent-1"


def test_probe_call_errors_are_recorded_per_call(make_ctx):
    http = FakeHttp({("gateway", "/health"): HttpCallError("ConnectTimeout: timed out")})

    result = Probe().run(
        {"target": "gateway", "path": "/health", "times": 2}, make_ctx(_k8s(), http)
    )

    assert result.ok
    assert result.data["fail"] == 2
    assert result.data["calls"][0] == {
        "status": None,
        "latency_ms": None,
        "error": "ConnectTimeout: timed out",
    }


def test_probe_unknown_target_fails(make_ctx):
    result = Probe().run({"target": "nope", "path": "/health"}, make_ctx(_k8s(), FakeHttp()))

    assert not result.ok
    assert "nope" in result.summary


def test_probe_pod_target_via_service_is_rejected(make_ctx):
    result = Probe().run({"target": "agent-1", "path": "/health"}, make_ctx(_k8s(), FakeHttp()))

    assert not result.ok
    assert "via=pod" in result.summary


@pytest.mark.parametrize(
    "args",
    [
        {"target": "gateway", "path": "/health", "times": 0},
        {"target": "gateway", "path": "/health", "times": 11},
        {"target": "gateway", "path": "//evil.com"},
        {"target": "gateway", "path": "health"},
        {"target": "gateway", "path": "/health", "via": "node"},
    ],
)
def test_probe_rejects_bad_input(make_ctx, args):
    with pytest.raises(ServiceException):
        Probe().run(args, make_ctx(_k8s(), FakeHttp()))


def test_probe_rejects_external_name_service(make_ctx):
    external = service("ext")
    external["spec"] = {"type": "ExternalName", "externalName": "api.example.com"}
    k8s = FakeK8s({"Service": [external], "Pod": []})
    http = FakeHttp()

    result = Probe().run({"target": "ext", "path": "/health"}, make_ctx(k8s, http))

    assert not result.ok
    assert "ExternalName" in result.summary
    assert http.calls == []


def test_probe_via_pod_keeps_pod_and_port_per_call(make_ctx):
    result = Probe().run(
        {"target": "gateway", "path": "/health", "via": "pod", "times": 1},
        make_ctx(_k8s(), FakeHttp()),
    )

    assert result.data["calls"][0] == {
        "pod": "gateway-1",
        "port": 8080,
        "status": 200,
        "latency_ms": 12.0,
    }


def test_probe_total_calls_are_capped(make_ctx):
    pods = [pod(f"gateway-{i}", ip=f"10.0.1.{i}") for i in range(5)]
    k8s = FakeK8s({"Service": [service("gateway")], "Pod": pods})
    http = FakeHttp()

    result = Probe().run(
        {"target": "gateway", "path": "/health", "via": "pod", "times": 10}, make_ctx(k8s, http)
    )

    assert len(http.calls) == probe_module.MAX_CALLS
    assert result.truncated
    assert "멈춤" in result.summary


def test_probe_stops_at_deadline(make_ctx, monkeypatch):
    ticks = iter([0.0, 0.0, 10.0, 31.0])
    monkeypatch.setattr(probe_module, "_clock", lambda: next(ticks))
    http = FakeHttp()

    result = Probe().run(
        {"target": "gateway", "path": "/health", "times": 5}, make_ctx(_k8s(), http)
    )

    assert len(http.calls) == 2
    assert result.truncated


def test_probe_service_without_selector_says_why(make_ctx):
    k8s = FakeK8s({"Service": [service("gateway", selector={})], "Pod": [pod("gateway-1")]})

    result = Probe().run(
        {"target": "gateway", "path": "/health", "via": "pod"}, make_ctx(k8s, FakeHttp())
    )

    assert "selector가 없음" in result.summary


@pytest.mark.parametrize(
    "path", ["/health", "/healthz", "/api/v1/health", "/ready", "/readyz", "/livez"]
)
def test_probe_allows_health_paths(make_ctx, path):
    http = FakeHttp()

    result = Probe().run({"target": "gateway", "path": path, "times": 1}, make_ctx(_k8s(), http))

    assert result.ok
    assert http.calls == [("GET", "gateway", 80, path)]


@pytest.mark.parametrize("path", ["/", "/api/v1/agent/invoke", "/health/../admin", "/health?x=1"])
def test_probe_rejects_paths_outside_health_and_readiness(make_ctx, path):
    http = FakeHttp()

    result = Probe().run({"target": "gateway", "path": path, "times": 1}, make_ctx(_k8s(), http))

    assert not result.ok
    assert "허용" in result.summary
    assert http.calls == []


def _with_readiness(p, path):
    p["spec"]["containers"][0]["readinessProbe"] = {"httpGet": {"path": path, "port": 8080}}
    return p


def test_probe_allows_readiness_probe_path_of_target_pods(make_ctx):
    k8s = FakeK8s(
        {
            "Service": [service("gateway")],
            "Pod": [_with_readiness(pod("gateway-1"), "/health/ready")],
        }
    )
    http = FakeHttp()

    by_service = Probe().run(
        {"target": "gateway", "path": "/health/ready", "times": 1}, make_ctx(k8s, http)
    )
    by_pod = Probe().run(
        {"target": "gateway-1", "path": "/health/ready", "via": "pod", "times": 1},
        make_ctx(k8s, http),
    )

    assert by_service.ok and by_pod.ok
    assert len(http.calls) == 2


def test_probe_rejects_readiness_path_of_other_pods(make_ctx):
    k8s = FakeK8s(
        {
            "Service": [service("gateway")],
            "Pod": [
                pod("gateway-1"),
                _with_readiness(pod("agent-1", app="agent"), "/agent/ready"),
            ],
        }
    )
    http = FakeHttp()

    result = Probe().run(
        {"target": "gateway", "path": "/agent/ready", "times": 1}, make_ctx(k8s, http)
    )

    assert not result.ok
    assert http.calls == []
