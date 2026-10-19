"""run_api_scenario·query_app_api: 앱 HTTP API 호출 (상세설계 §3.2 5·6번).

- query_app_api: 설정 `app_apis` 허용 목록에 있는 (앱, 경로)만 GET으로 부른다.
  그 밖은 호출 전에 거부한다.
- run_api_scenario: 설정 `api_scenarios`에 등록한 시나리오를 이름으로만 실행한다.
  단계는 설정에서 오고 LLM은 이름만 고른다. 첫 실패 단계에서 멈춘다.
  로그인 단계의 테스트 계정은 `ctx.http.account(<Secret 이름>)`로 받고 결과에 넣지 않는다.

호출은 `ctx.http`(대상 네임스페이스의 Service만 부르는 클라이언트)로 한다.
포트는 `ctx.k8s`로 읽은 Service의 첫 번째 포트(단계에 `port`가 있으면 그 값)를 쓴다.
응답 본문은 필드 수·길이를 줄이고, 마스킹은 Tool.run이 한다.
"""

from typing import Any

from pydantic import BaseModel, Field

from core.config import ApiScenario
from core.error.service_exception import ErrorCode, ServiceException
from core.http import HttpCallError, HttpResponse
from service.tools._common import HttpPath, K8sName, by_name, clip, read, require, service_port
from service.tools.base import Tool, ToolContext, ToolObservation

MAX_KEYS, MAX_LIST, MAX_DEPTH = 30, 20, 4
MAX_BODY_TEXT = 500
MAX_ERROR_TEXT = 200
DEFAULT_STEP_TIMEOUT_S = 10.0
_TOKEN_FIELDS = ("access_token", "accessToken", "token")


def _port(ctx: ToolContext, service: str) -> int:
    svc = by_name(require(read(ctx, "Service"), "Service"), service)
    if svc is None:
        raise ServiceException(ErrorCode.NOT_FOUND, f"Service {service}이 대상 네임스페이스에 없음")
    return service_port(svc)


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
        name: str = Field(
            pattern=r"^[A-Za-z0-9_-]{1,64}$", description="설정에 등록한 시나리오 이름"
        )

    def __init__(self, scenarios: list[ApiScenario]):
        self._scenarios = {s.name: s for s in scenarios}
        names = ", ".join(self._scenarios) or "없음"
        self.description = f"{type(self).description} 등록된 시나리오 — {names}"

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        scenario = self._scenarios.get(args.name)
        if scenario is None:
            raise ServiceException(
                ErrorCode.VALIDATION_ERROR, f"등록되지 않은 시나리오: {args.name}"
            )
        steps: list[dict[str, Any]] = []
        headers: dict[str, str] = {}
        failed = None
        for i, step in enumerate(scenario.steps, start=1):
            [(action, spec)] = step.items()
            result, token = _run_step(ctx, action, spec, headers)
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


def _run_step(
    ctx: ToolContext, action: str, spec: dict[str, Any], headers: dict[str, str]
) -> tuple[dict[str, Any], str | None]:
    """단계 하나를 실행한다. (결과, 로그인 토큰)을 돌려준다. 토큰은 결과에 넣지 않는다."""
    if action not in ("login", "call"):
        raise ValueError(f"모르는 시나리오 단계: {action}")
    via, path = spec["via"], spec["path"]
    port = spec.get("port") or _port(ctx, via)
    timeout = float(spec.get("timeout_s", DEFAULT_STEP_TIMEOUT_S))
    method = (
        "POST" if action == "login" else spec.get("method", "POST" if "body" in spec else "GET")
    )
    out: dict[str, Any] = {"action": action, "via": via, "path": path, "method": method}
    try:
        if action == "login":
            account = ctx.http.account(spec["account_secret"])
            resp = ctx.http.post(service=via, port=port, path=path, json=account, timeout_s=timeout)
        elif method == "GET":
            resp = ctx.http.get(service=via, port=port, path=path, timeout_s=timeout)
        elif method == "POST":
            resp = ctx.http.post(
                service=via,
                port=port,
                path=path,
                json=spec.get("body"),
                headers=headers or None,
                timeout_s=timeout,
            )
        else:
            raise ValueError(f"시나리오 단계는 GET·POST만 쓴다: {method}")
    except HttpCallError as e:
        return {**out, "status": None, "latency_ms": None, "ok": False, "error": str(e)}, None

    expect = spec.get("expect")
    ok = resp.status == expect if expect else 200 <= resp.status < 300
    out.update(status=resp.status, latency_ms=resp.elapsed_ms, ok=ok)
    if not ok:
        out["error"] = clip(resp.text, MAX_ERROR_TEXT)
    token = None
    if action == "login" and ok:
        payload = resp.json()
        if isinstance(payload, dict):
            token = next((payload[k] for k in _TOKEN_FIELDS if payload.get(k)), None)
    return out, token


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
