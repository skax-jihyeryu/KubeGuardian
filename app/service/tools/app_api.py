"""run_api_scenario·query_app_api: 앱 HTTP API 호출 (상세설계 §3.2 5·6번).

- query_app_api: 설정 `app_apis` 허용 목록에 있는 (앱, 경로)만 GET으로 부른다.
  그 밖은 호출 전에 거부한다.
- run_api_scenario: 설정 `api_scenarios`에 등록한 시나리오를 이름으로만 실행한다.
  단계는 설정에서 오고 LLM은 이름만 고른다. 첫 실패 단계에서 멈춘다.
  로그인 단계의 테스트 계정은 `ctx.http.account(<Secret 이름>)`로 받고 결과에 넣지 않는다.
  단계 형식(`core.config.ApiScenarioStep`): login은 응답 JSON 경로(`token`)에서 토큰을 꺼내
  뒤 단계에 `Authorization: Bearer`로 붙이고, `extract`로 꺼낸 값은 뒤 단계 body의 "${이름}"에
  넣는다. `check`는 HTTP 200이어도 응답 값이 실패 문구로 시작하면 실패로 본다.

호출은 `ctx.http`(대상 네임스페이스의 Service만 부르는 클라이언트)로 한다.
포트는 `ctx.k8s`로 읽은 Service의 첫 번째 포트(단계에 `port`가 있으면 그 값)를 쓴다.
응답 본문은 필드 수·길이를 줄이고, 마스킹은 Tool.run이 한다.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from core.config import (
    SCENARIO_NAME_PATTERN,
    ApiScenario,
    ApiScenarioStep,
    CallStep,
    LoginStep,
    ResponseCheck,
)
from core.error.service_exception import ErrorCode, ServiceException
from core.http import HttpCallError, HttpResponse
from service.tools._common import (
    HttpPath,
    K8sName,
    by_name,
    clip,
    ensure_in_namespace,
    read,
    require,
    service_port,
)
from service.tools.base import Tool, ToolContext, ToolObservation

MAX_KEYS, MAX_LIST, MAX_DEPTH = 30, 20, 4
MAX_BODY_TEXT = 500
MAX_ERROR_TEXT = 200
_TOKEN_FIELDS = ("access_token", "accessToken", "token")


def _port(ctx: ToolContext, service: str, port: int | None = None) -> int:
    """대상 네임스페이스의 Service인지(ExternalName 아님) 확인하고 포트를 정한다."""
    svc = by_name(require(read(ctx, "Service"), "Service"), service)
    if svc is None:
        raise ServiceException(ErrorCode.NOT_FOUND, f"Service {service}이 대상 네임스페이스에 없음")
    ensure_in_namespace(svc)
    return port or service_port(svc)


class QueryAppApi(Tool):
    name = "query_app_api"
    description = (
        "앱이 제공하는 상태 API를 GET으로 조회한다. Kubernetes에는 흔적이 없는 앱 층 원인"
        "(예: llm-gateway 할당량 소진)을 확인할 때 쓰세요. "
        "설정의 허용 목록에 있는 앱·경로만 부를 수 있다."
    )
    failure_kind = "app_api"

    class Input(BaseModel):
        app: K8sName = Field(description="앱(Service) 이름, 예: llm-gateway")
        path: HttpPath = Field(description="허용 목록의 경로")

    def __init__(self, app_apis: dict[str, list[str]]):
        self._allow = app_apis
        allowed = "; ".join(f"{app}: {', '.join(paths)}" for app, paths in app_apis.items())
        self.description = f"{type(self).description} 허용 경로 — {allowed or '없음'}"

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        if args.path not in self._allow.get(args.app, []):
            raise ServiceException(
                ErrorCode.VALIDATION_ERROR, f"허용 목록에 없는 앱·경로: {args.app} {args.path}"
            )
        port = _port(ctx, args.app)
        resp = ctx.http.get(service=args.app, port=port, path=args.path)
        body, truncated = _body(resp)
        return ToolObservation(
            kind="app_api",
            subject=f"{ctx.namespace}/Service/{args.app}",
            source=f"GET http://{args.app}:{port}{args.path}",
            summary=f"{args.app} GET {args.path} → {resp.status} ({round(resp.elapsed_ms)}ms)",
            data={"status": resp.status, "latency_ms": resp.elapsed_ms, "body": body},
            truncated=truncated or resp.truncated,
        )


class RunApiScenario(Tool):
    name = "run_api_scenario"
    description = (
        "실제 사용자 흐름(예: 로그인 → 채팅)을 gateway API로 순서대로 호출해 "
        "어느 단계에서 실패하는지 본다. 사용자 증상을 재현하거나, "
        "Kubernetes 상태는 정상인데 사용자가 실패할 때 쓰세요. "
        "클러스터는 바꾸지 않지만 앱에 로그인·채팅 기록이 남는다."
    )
    failure_kind = "app_api"

    class Input(BaseModel):
        name: str = Field(pattern=SCENARIO_NAME_PATTERN, description="설정에 등록한 시나리오 이름")

    def __init__(self, scenarios: list[ApiScenario]):
        self._scenarios = {s.name: s for s in scenarios}
        names = ", ".join(self._scenarios) or "없음"
        self.description = f"{type(self).description} 등록된 시나리오 — {names}"

    @property
    def scenario_names(self) -> list[str]:
        return list(self._scenarios)

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        scenario = self._scenarios.get(args.name)
        if scenario is None:
            raise ServiceException(
                ErrorCode.VALIDATION_ERROR, f"등록되지 않은 시나리오: {args.name}"
            )
        steps: list[dict[str, Any]] = []
        headers: dict[str, str] = {}
        variables: dict[str, Any] = {}  # extract로 꺼낸 값. 결과에 넣지 않는다
        failed = None
        for i, step in enumerate(scenario.steps, start=1):
            result, token = _run_step(ctx, step, headers, variables)
            steps.append({"step": i, **result})
            if token:
                headers = {"Authorization": f"Bearer {token}"}
            if not result["ok"]:
                failed = i
                break

        total = round(sum(s["latency_ms"] or 0 for s in steps))
        if failed is None:
            summary = f"시나리오 {args.name}: {len(scenario.steps)}단계 모두 성공 (총 {total}ms)"
        else:
            bad = steps[-1]
            outcome = bad["status"] if bad["status"] is not None else bad.get("error")
            if bad.get("reason"):
                outcome = f"{outcome}, {bad['reason']}"
            summary = (
                f"시나리오 {args.name}: {len(scenario.steps)}단계 중 {failed}단계 실패 "
                f"({bad['action']} {bad['path']} → {outcome})"
            )
        return ToolObservation(
            kind="app_api",
            subject=f"{ctx.namespace}/Scenario/{args.name}",
            source=f"scenario {args.name}",
            summary=summary,
            data={"scenario": args.name, "failed_step": failed, "steps": steps},
        )


class _StepFailed(Exception):
    """응답은 받았지만 단계 조건(토큰·extract·check)을 못 채움. reason은 응답 값을 담지 않는다."""

    def __init__(self, reason: str, error: str | None = None):
        super().__init__(reason)
        self.reason, self.error = reason, error or reason


def _run_step(
    ctx: ToolContext,
    step: ApiScenarioStep,
    headers: dict[str, str],
    variables: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """단계 하나를 실행한다. (결과, 로그인 토큰)을 돌려준다. 토큰·변수 값은 결과에 넣지 않는다."""
    spec: LoginStep | CallStep = step.login or step.call  # type: ignore[assignment]
    action = "login" if step.login else "call"
    # call의 기본 메서드는 POST다 (예시 설정의 /agent/invoke). 조회 단계는 method: GET을 적는다
    method = "POST" if isinstance(spec, LoginStep) else spec.method
    out: dict[str, Any] = {"action": action, "via": spec.via, "path": spec.path, "method": method}
    port = _port(ctx, spec.via, spec.port)
    send = {"service": spec.via, "port": port, "path": spec.path, "timeout_s": spec.timeout_s}
    try:
        if isinstance(spec, LoginStep):
            account = ctx.http.account(spec.account_secret)
            resp = ctx.http.post(**send, json=account)
        elif method == "GET":
            resp = ctx.http.get(**send, headers=headers or None)
        else:
            body = _substitute(spec.body, variables)
            resp = ctx.http.post(**send, json=body, headers=headers or None)
    except (HttpCallError, _StepFailed) as e:
        return {**out, "status": None, "latency_ms": None, "ok": False, "error": str(e)}, None

    ok = resp.status == spec.expect if spec.expect else 200 <= resp.status < 300
    out.update(status=resp.status, latency_ms=resp.elapsed_ms, ok=ok)
    if not ok:
        out["error"] = _failure_text(resp, login=isinstance(spec, LoginStep))
        return out, None
    try:
        payload = resp.json()
        token = _token(spec, payload) if isinstance(spec, LoginStep) else None
        for name, path in spec.extract.items():
            variables[name] = _required(payload, path)
        if isinstance(spec, CallStep) and spec.check:
            _check(spec.check, payload)
    except _StepFailed as e:
        out.update(ok=False, reason=e.reason, error=e.error)
        return out, None
    return out, token


def _failure_text(resp: HttpResponse, *, login: bool) -> str:
    """실패 응답의 오류 문구. 로그인은 본문을 버린다 (422가 보낸 계정 정보를 되돌려 주기 때문).

    call은 FastAPI 검증 오류의 detail[].input(요청 본문 사본)을 지운 뒤 줄여 쓴다.
    """
    if login:
        return f"HTTP {resp.status}"
    payload = resp.json()
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, list):
        return clip(resp.text, MAX_ERROR_TEXT)
    cleaned = [
        {k: v for k, v in d.items() if k != "input"} if isinstance(d, dict) else d for d in detail
    ]
    return clip(json.dumps({**payload, "detail": cleaned}, ensure_ascii=False), MAX_ERROR_TEXT)


def _token(spec: LoginStep, payload: Any) -> str | None:
    if spec.token is None:  # 경로를 적지 않으면 흔한 필드 이름에서 찾고, 없어도 실패로 보지 않는다
        if not isinstance(payload, dict):
            return None
        return next((payload[k] for k in _TOKEN_FIELDS if isinstance(payload.get(k), str)), None)
    token = _required(payload, spec.token)
    if not isinstance(token, str):
        raise _StepFailed(f"응답의 {spec.token}이 문자열이 아님")
    return token


def _json_path(payload: Any, path: str) -> Any:
    """점으로 이은 경로의 값. 없으면 None. 목록 위치는 숫자로 쓴다."""
    value = payload
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def _required(payload: Any, path: str) -> Any:
    value = _json_path(payload, path)
    if value is None:
        raise _StepFailed(f"응답에 {path} 없음")
    return value


def _check(check: ResponseCheck, payload: Any) -> None:
    value = _json_path(payload, check.path)
    if not isinstance(value, str):
        raise _StepFailed(f"응답에 {check.path} 없음 또는 문자열이 아님")
    if value.startswith(check.not_starts_with):
        reason = f"{check.path}가 '{check.not_starts_with}'로 시작"
        raise _StepFailed(reason, clip(value, MAX_ERROR_TEXT))


_VARIABLE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    """값이 "${이름}"인 문자열을 앞 단계 extract 값으로 바꾼다 (타입 유지). 없는 이름은 실패."""
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    if isinstance(value, str) and (m := _VARIABLE.match(value)):
        if m[1] not in variables:
            raise _StepFailed(f"앞 단계에서 꺼내지 않은 변수: {m[1]}")
        return variables[m[1]]
    return value


def _body(resp: HttpResponse) -> tuple[Any, bool]:
    payload = resp.json()
    if payload is None:
        return clip(resp.text, MAX_BODY_TEXT), len(resp.text) > MAX_BODY_TEXT
    return _limit(payload, 0)


def _limit(value: Any, depth: int) -> tuple[Any, bool]:
    """JSON 값의 키 수·목록 길이·문자열 길이·깊이를 줄인다. (결과, 잘렸는지)."""
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return "…", True
        out, cut = {}, len(value) > MAX_KEYS
        for k in list(value)[:MAX_KEYS]:
            out[k], c = _limit(value[k], depth + 1)
            cut = cut or c
        return out, cut
    if isinstance(value, list):
        if depth >= MAX_DEPTH:
            return "…", True
        items = [_limit(v, depth + 1) for v in value[:MAX_LIST]]
        return [v for v, _ in items], len(value) > MAX_LIST or any(c for _, c in items)
    if isinstance(value, str):
        short = clip(value)
        return short, short != value
    return value, False
