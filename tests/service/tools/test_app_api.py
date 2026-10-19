import pytest

from core.config import ApiScenario
from core.error.service_exception import ServiceException
from core.http import HttpCallError, HttpResponse
from service.tools.app_api import QueryAppApi, RunApiScenario
from tests.service.tools.fakes import FakeHttp, FakeK8s, service

ALLOW = {"llm-gateway": ["/api/v1/health", "/api/v1/quota"]}


def _k8s():
    return FakeK8s({"Service": [service("llm-gateway", port=8000), service("gateway", port=80)]})


# ── query_app_api ──


def test_query_app_api_gets_allowlisted_path(make_ctx, evidence):
    body = '{"remaining": 0, "limit": 100, "api_key": "sk-abcdefghijklmnopqrstuvwx"}'
    http = FakeHttp({("llm-gateway", "/api/v1/quota"): HttpResponse(200, 34.0, body)})

    result = QueryAppApi(ALLOW).run(
        {"app": "llm-gateway", "path": "/api/v1/quota"}, make_ctx(_k8s(), http)
    )

    assert result.ok
    assert http.calls == [("GET", "llm-gateway", 8000, "/api/v1/quota")]
    assert result.data["status"] == 200
    assert result.data["body"]["remaining"] == 0
    assert "sk-abc" not in result.model_dump_json()
    saved = evidence.saved[0]
    assert saved.kind == "app_api"
    assert saved.subject == "dev/Service/llm-gateway"
    assert result.summary == "llm-gateway GET /api/v1/quota → 200 (34ms)"


@pytest.mark.parametrize(
    "args",
    [
        {"app": "llm-gateway", "path": "/api/v1/admin"},
        {"app": "llm-gateway", "path": "/api/v1/quota?x=1"},
        {"app": "gateway", "path": "/api/v1/health"},
    ],
)
def test_query_app_api_rejects_paths_outside_allowlist(make_ctx, args):
    http = FakeHttp()

    result = QueryAppApi(ALLOW).run(args, make_ctx(_k8s(), http))

    assert not result.ok
    assert "허용 목록" in result.summary
    assert http.calls == []


def test_query_app_api_has_no_method_argument():
    assert "method" not in QueryAppApi.Input.model_fields


def test_query_app_api_description_lists_allowlist():
    assert "llm-gateway: /api/v1/health, /api/v1/quota" in QueryAppApi(ALLOW).description


def test_query_app_api_limits_body(make_ctx):
    big = "{" + ",".join(f'"k{i}": "{"x" * 500}"' for i in range(50)) + "}"
    http = FakeHttp({("llm-gateway", "/api/v1/health"): HttpResponse(200, 1.0, big)})

    result = QueryAppApi(ALLOW).run(
        {"app": "llm-gateway", "path": "/api/v1/health"}, make_ctx(_k8s(), http)
    )

    assert len(result.data["body"]) == 30
    assert len(result.data["body"]["k0"]) < 400
    assert result.truncated


def test_query_app_api_text_body_and_call_failure(make_ctx):
    http = FakeHttp(
        {
            ("llm-gateway", "/api/v1/health"): HttpResponse(503, 2.0, "upstream down"),
            ("llm-gateway", "/api/v1/quota"): HttpCallError("ConnectError: refused"),
        }
    )
    ctx = make_ctx(_k8s(), http)

    text = QueryAppApi(ALLOW).run({"app": "llm-gateway", "path": "/api/v1/health"}, ctx)
    failed = QueryAppApi(ALLOW).run({"app": "llm-gateway", "path": "/api/v1/quota"}, ctx)

    assert text.data["body"] == "upstream down"
    assert not failed.ok
    assert "ConnectError" in failed.summary


# ── run_api_scenario ──

SCENARIOS = [
    ApiScenario(
        name="chat",
        steps=[
            {
                "login": {
                    "via": "gateway",
                    "path": "/api/v1/auth/login",
                    "account_secret": "kg-test-user",
                }
            },
            {
                "call": {
                    "via": "gateway",
                    "path": "/api/v1/agent/invoke",
                    "method": "POST",
                    "body": {"message": "hi"},
                    "expect": 200,
                    "timeout_s": 60,
                }
            },
        ],
    )
]
ACCOUNTS = {"kg-test-user": {"username": "kg", "password": "hunter2"}}


def _scenario_http(invoke):
    return FakeHttp(
        {
            ("gateway", "/api/v1/auth/login"): HttpResponse(
                200, 10.0, '{"access_token": "tok-abcdefgh12345678"}'
            ),
            ("gateway", "/api/v1/agent/invoke"): invoke,
        },
        accounts=ACCOUNTS,
    )


def test_scenario_runs_steps_in_order_with_login_token(make_ctx, evidence):
    http = _scenario_http(HttpResponse(200, 300.0, '{"answer": "ok"}'))

    result = RunApiScenario(SCENARIOS).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.ok
    login, call = http.calls
    assert login[:5] == ("POST", "gateway", 80, "/api/v1/auth/login", ACCOUNTS["kg-test-user"])
    assert call[5] == {"Authorization": "Bearer tok-abcdefgh12345678"}
    assert call[4] == {"message": "hi"}
    assert result.data["failed_step"] is None
    assert [s["status"] for s in result.data["steps"]] == [200, 200]
    dumped = result.model_dump_json() + evidence.saved[0].model_dump_json()
    assert "hunter2" not in dumped and "tok-abcdefgh" not in dumped
    assert evidence.saved[0].subject == "dev/Scenario/chat"
    assert result.summary == "시나리오 chat: 2단계 모두 성공 (총 310ms)"


def test_scenario_stops_at_failed_step_with_error_summary(make_ctx):
    http = _scenario_http(HttpResponse(500, 5.0, '{"detail": "quota exceeded"}'))

    result = RunApiScenario(SCENARIOS).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.ok
    assert result.data["failed_step"] == 2
    assert result.data["steps"][1]["error"] == '{"detail": "quota exceeded"}'
    assert result.summary == "시나리오 chat: 2단계 중 2단계 실패 (call /api/v1/agent/invoke → 500)"


def test_scenario_call_error_is_a_failed_step(make_ctx):
    http = _scenario_http(HttpCallError("ReadTimeout: timed out"))

    result = RunApiScenario(SCENARIOS).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] == 2
    assert result.data["steps"][1]["error"] == "ReadTimeout: timed out"


def test_unknown_scenario_is_rejected_without_calls(make_ctx):
    http = _scenario_http(200)

    result = RunApiScenario(SCENARIOS).run({"name": "other"}, make_ctx(_k8s(), http))

    assert not result.ok
    assert http.calls == []


def test_scenario_input_is_name_only():
    assert set(RunApiScenario.Input.model_fields) == {"name"}
    assert "chat" in RunApiScenario(SCENARIOS).description


def test_scenario_rejects_unknown_step_action(make_ctx):
    bad = [ApiScenario(name="bad", steps=[{"delete": {"via": "gateway", "path": "/x"}}])]

    result = RunApiScenario(bad).run({"name": "bad"}, make_ctx(_k8s(), _scenario_http(200)))

    assert not result.ok


def test_scenario_name_validation(make_ctx):
    with pytest.raises(ServiceException):
        RunApiScenario(SCENARIOS).run({"name": "a b"}, make_ctx(_k8s(), FakeHttp()))
