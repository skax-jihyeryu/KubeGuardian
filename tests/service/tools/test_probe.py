import pytest

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
        {"target": "agent-1", "path": "/", "via": "pod", "times": 1}, make_ctx(_k8s(), http)
    )

    assert http.calls == [("GET", "10.0.0.7", 9000, "/")]
    assert result.data["pods"] == {"agent-1": {"ok": 1, "fail": 0}}
    assert evidence.saved[0].subject == "dev/Pod/agent-1"


def test_probe_call_errors_are_recorded_per_call(make_ctx):
    http = FakeHttp({("gateway", "/"): HttpCallError("ConnectTimeout: timed out")})

    result = Probe().run({"target": "gateway", "path": "/", "times": 2}, make_ctx(_k8s(), http))

    assert result.ok
    assert result.data["fail"] == 2
    assert result.data["calls"][0] == {
        "status": None,
        "latency_ms": None,
        "error": "ConnectTimeout: timed out",
    }


def test_probe_unknown_target_fails(make_ctx):
    result = Probe().run({"target": "nope", "path": "/"}, make_ctx(_k8s(), FakeHttp()))

    assert not result.ok
    assert "nope" in result.summary


def test_probe_pod_target_via_service_is_rejected(make_ctx):
    result = Probe().run({"target": "agent-1", "path": "/"}, make_ctx(_k8s(), FakeHttp()))

    assert not result.ok
    assert "via=pod" in result.summary


@pytest.mark.parametrize(
    "args",
    [
        {"target": "gateway", "path": "/", "times": 0},
        {"target": "gateway", "path": "/", "times": 11},
        {"target": "gateway", "path": "//evil.com"},
        {"target": "gateway", "path": "health"},
        {"target": "gateway", "path": "/", "via": "node"},
    ],
)
def test_probe_rejects_bad_input(make_ctx, args):
    with pytest.raises(ServiceException):
        Probe().run(args, make_ctx(_k8s(), FakeHttp()))
