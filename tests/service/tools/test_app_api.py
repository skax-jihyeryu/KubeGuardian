import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import ApiScenario, load_kubeguardian_config
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


@pytest.mark.parametrize(
    "step",
    [
        {"delete": {"via": "gateway", "path": "/x"}},
        {},
        {
            "login": {"via": "gateway", "path": "/l", "account_secret": "s"},
            "call": {"via": "gateway", "path": "/x"},
        },
        {"call": {"via": "gateway", "path": "/x", "method": "DELETE"}},
        {"call": {"via": "gateway", "path": "/x", "check": {"path": "a"}}},
    ],
)
def test_scenario_step_format_is_validated_at_config_load(step):
    with pytest.raises(ValidationError):
        ApiScenario(name="bad", steps=[step])


def test_scenario_name_validation(make_ctx):
    with pytest.raises(ServiceException):
        RunApiScenario(SCENARIOS).run({"name": "a b"}, make_ctx(_k8s(), FakeHttp()))


def test_query_app_api_rejects_external_name_service(make_ctx):
    external = service("llm-gateway")
    external["spec"] = {"type": "ExternalName", "externalName": "x.other-ns.svc.cluster.local"}
    http = FakeHttp()

    result = QueryAppApi(ALLOW).run(
        {"app": "llm-gateway", "path": "/api/v1/health"},
        make_ctx(FakeK8s({"Service": [external]}), http),
    )

    assert not result.ok
    assert http.calls == []


# 템플릿 API 응답 (scripts/target/smoke.sh와 같은 흐름). 로그인은 success 래퍼가 없다
TEMPLATE_LOGIN = (
    '{"access_token": "tok-abcdefgh12345678", "user": {"user_id": "kg",'
    ' "role": {"organizations": [{"org_id": 7}]}}}'
)


def _template_http(answer):
    body = json.dumps({"success": True, "data": {"answer": answer}}, ensure_ascii=False)
    return FakeHttp(
        {
            ("gateway", "/api/v1/auth/login"): HttpResponse(200, 10.0, TEMPLATE_LOGIN),
            ("gateway", "/api/v1/agent/invoke"): HttpResponse(200, 900.0, body),
        },
        accounts={"kg-test-user": {"id": "kg", "password": "hunter2"}},
    )


def _example_scenarios():
    example = Path(__file__).parents[3] / "kubeguardian.example.yaml"
    return load_kubeguardian_config(example).api_scenarios


def test_example_config_scenario_follows_template_api(make_ctx, evidence):
    http = _template_http("안녕하세요. 저는 도우미입니다.")

    result = RunApiScenario(_example_scenarios()).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] is None
    login, invoke = http.calls
    assert login[:5] == (
        "POST",
        "gateway",
        80,
        "/api/v1/auth/login",
        {"id": "kg", "password": "hunter2"},
    )
    assert invoke[:4] == ("POST", "gateway", 80, "/api/v1/agent/invoke")
    assert invoke[4]["org_id"] == 7  # 로그인 응답에서 꺼낸 값
    assert set(invoke[4]) == {"query", "model", "org_id"}
    assert invoke[5] == {"Authorization": "Bearer tok-abcdefgh12345678"}
    dumped = result.model_dump_json() + evidence.saved[0].model_dump_json()
    assert "hunter2" not in dumped and "tok-abcdefgh" not in dumped


def test_example_config_scenario_fails_on_template_error_answer_with_200(make_ctx):
    http = _template_http("오류가 발생했습니다: Connection error.")

    result = RunApiScenario(_example_scenarios()).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] == 2
    bad = result.data["steps"][1]
    assert bad["status"] == 200 and not bad["ok"]
    assert "오류가 발생했습니다: Connection error." in bad["error"]
    assert "data.answer" in result.summary


def _steps(*steps):
    return [ApiScenario(name="s", steps=list(steps))]


LOGIN = {"via": "gateway", "path": "/api/v1/auth/login", "account_secret": "kg-test-user"}


def test_login_token_is_read_from_json_path(make_ctx):
    scenarios = _steps(
        {"login": {**LOGIN, "token": "data.jwt"}},
        {"call": {"via": "gateway", "path": "/me", "method": "GET"}},
    )
    http = _scenario_http(200)
    http.responses[("gateway", "/api/v1/auth/login")] = HttpResponse(
        200, 1.0, '{"data": {"jwt": "tok-abcdefgh12345678"}}'
    )

    result = RunApiScenario(scenarios).run({"name": "s"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] is None
    assert http.headers == [{"Authorization": "Bearer tok-abcdefgh12345678"}]


def test_login_fails_when_configured_token_path_is_missing(make_ctx):
    scenarios = _steps({"login": {**LOGIN, "token": "data.jwt"}})

    result = RunApiScenario(scenarios).run({"name": "s"}, make_ctx(_k8s(), _scenario_http(200)))

    assert result.data["failed_step"] == 1
    assert "data.jwt" in result.data["steps"][0]["error"]


def test_missing_extract_path_fails_the_step(make_ctx):
    scenarios = _steps({"login": {**LOGIN, "extract": {"org_id": "user.role.organizations.0"}}})

    result = RunApiScenario(scenarios).run({"name": "s"}, make_ctx(_k8s(), _scenario_http(200)))

    assert result.data["failed_step"] == 1
    assert "user.role.organizations.0" in result.data["steps"][0]["error"]


def test_unknown_variable_in_body_fails_the_step_without_call(make_ctx):
    scenarios = _steps({"call": {"via": "gateway", "path": "/x", "body": {"org": "${org_id}"}}})
    http = _scenario_http(200)

    result = RunApiScenario(scenarios).run({"name": "s"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] == 1
    assert "org_id" in result.data["steps"][0]["error"]
    assert http.calls == []


def test_check_fails_when_answer_is_missing(make_ctx):
    check = {"path": "data.answer", "not_starts_with": "오류가 발생했습니다:"}
    scenarios = _steps({"call": {"via": "gateway", "path": "/x", "check": check}})
    http = _scenario_http(200)
    http.responses[("gateway", "/x")] = HttpResponse(200, 1.0, '{"success": true}')

    result = RunApiScenario(scenarios).run({"name": "s"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] == 1


def test_get_step_sends_login_token_and_ignores_non_string_token(make_ctx):
    steps = [
        {"login": {"via": "gateway", "path": "/login", "account_secret": "kg-test-user"}},
        {"call": {"via": "gateway", "path": "/me", "method": "GET"}},
    ]
    scenarios = [ApiScenario(name="me", steps=steps)]
    ok_http = FakeHttp(
        {("gateway", "/login"): HttpResponse(200, 1.0, '{"token": "tok-abcdefgh12345678"}')},
        accounts=ACCOUNTS,
    )
    odd_http = FakeHttp(
        {("gateway", "/login"): HttpResponse(200, 1.0, '{"token": {"v": 1}}')}, accounts=ACCOUNTS
    )

    RunApiScenario(scenarios).run({"name": "me"}, make_ctx(_k8s(), ok_http))
    RunApiScenario(scenarios).run({"name": "me"}, make_ctx(_k8s(), odd_http))

    assert ok_http.headers == [{"Authorization": "Bearer tok-abcdefgh12345678"}]
    assert odd_http.headers == [None]


def _fastapi_422(password: str) -> str:
    body = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "email"],
                "msg": "Field required",
                "input": {"username": "kg", "password": password, "pin": password},
            }
        ]
    }
    return json.dumps(body)


def test_login_failure_body_is_not_kept(make_ctx, evidence):
    http = _scenario_http(200)
    http.responses[("gateway", "/api/v1/auth/login")] = HttpResponse(
        422, 3.0, _fastapi_422("hunter2")
    )

    result = RunApiScenario(SCENARIOS).run({"name": "chat"}, make_ctx(_k8s(), http))

    assert result.data["failed_step"] == 1
    assert result.data["steps"][0]["status"] == 422
    dumped = result.model_dump_json() + evidence.saved[0].model_dump_json()
    assert "hunter2" not in dumped and "Field required" not in dumped
    assert "422" in result.summary


def test_call_failure_removes_detail_input_but_keeps_message(make_ctx, evidence):
    http = _scenario_http(HttpResponse(422, 3.0, _fastapi_422("s3cretvalue")))

    result = RunApiScenario(SCENARIOS).run({"name": "chat"}, make_ctx(_k8s(), http))

    error = result.data["steps"][1]["error"]
    assert "Field required" in error and "email" in error
    dumped = result.model_dump_json() + evidence.saved[0].model_dump_json()
    assert "s3cretvalue" not in dumped
