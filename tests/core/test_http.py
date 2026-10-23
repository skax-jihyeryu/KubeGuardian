import httpx
import pytest

from core.http import HttpCallError, NamespacedHttp


def _client(handler, **kwargs):
    return NamespacedHttp("dev", transport=httpx.MockTransport(handler), **kwargs)


def test_get_service_uses_namespace_bound_service_host():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"status": "ok"})

    resp = _client(handler).get(service="gateway", port=8080, path="/health")

    assert seen == [("GET", "http://gateway.dev.svc:8080/health")]
    assert resp.status == 200
    assert resp.json() == {"status": "ok"}
    assert resp.elapsed_ms >= 0


def test_get_pod_ip():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(503, text="down")

    resp = _client(handler).get(pod_ip="10.0.0.5", port=8080, path="/health?x=1")

    assert seen == ["http://10.0.0.5:8080/health?x=1"]
    assert resp.status == 503
    assert resp.json() is None
    assert resp.text == "down"


def test_post_sends_json_and_headers():
    seen = []

    def handler(request):
        seen.append((request.method, request.content, request.headers.get("authorization")))
        return httpx.Response(200, json={"token": "t"})

    _client(handler).post(
        service="gateway", port=80, path="/login", json={"u": "a"}, headers={"Authorization": "x"}
    )

    assert seen == [("POST", b'{"u":"a"}', "x")]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"service": "gateway.kube-system", "port": 80, "path": "/"},
        {"service": "Gateway", "port": 80, "path": "/"},
        {"service": "gateway", "port": 80, "path": "//evil.com/x"},
        {"service": "gateway", "port": 80, "path": "@evil.com/x"},
        {"service": "gateway", "port": 80, "path": "/a b"},
        {"service": "gateway", "port": 0, "path": "/"},
        {"pod_ip": "evil.com", "port": 80, "path": "/"},
        {"pod_ip": "127.0.0.1", "port": 80, "path": "/"},
        {"pod_ip": "169.254.169.254", "port": 80, "path": "/"},
        {"pod_ip": "0.0.0.0", "port": 80, "path": "/"},
        {"pod_ip": "::1", "port": 80, "path": "/"},
        {"port": 80, "path": "/"},
        {"service": "gateway", "pod_ip": "10.0.0.5", "port": 80, "path": "/"},
    ],
)
def test_rejects_targets_outside_namespace_or_bad_paths(kwargs):
    def handler(request):  # pragma: no cover - 호출되면 안 된다
        raise AssertionError(str(request.url))

    with pytest.raises(ValueError):
        _client(handler).get(**kwargs)


def test_redirects_are_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://evil.com/"})

    assert _client(handler).get(service="gateway", port=80, path="/").status == 302


def test_transport_error_is_http_call_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(HttpCallError, match="ConnectError"):
        _client(handler).get(service="gateway", port=80, path="/")


def test_body_is_size_limited():
    def handler(request):
        return httpx.Response(200, text="x" * 200_000)

    resp = _client(handler).get(service="gateway", port=80, path="/")

    assert len(resp.text) == 64 * 1024
    assert resp.truncated


def test_account_reads_injected_test_account():
    http = NamespacedHttp("dev", accounts={"kg-test-user": {"username": "u", "password": "p"}})

    assert http.account("kg-test-user") == {"username": "u", "password": "p"}
    with pytest.raises(KeyError, match="테스트 계정 Secret other 없음"):
        http.account("other")


def test_get_can_send_headers():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200)

    _client(handler).get(service="gateway", port=80, path="/", headers={"Authorization": "x"})

    assert seen == ["x"]


def test_proxy_environment_is_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp:3128")

    assert NamespacedHttp("dev")._client.trust_env is False
