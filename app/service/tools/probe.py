"""probe: 서비스 상태 확인 (상세설계 §3.2 4번).

대상 Service·Pod와 포트는 `ctx.k8s`로 읽고, 호출은 `ctx.http`(대상 네임스페이스에 묶인
HTTP 클라이언트)로 GET만 한다. Service 경유로 반복 호출하거나, Service가 고르는 Pod(또는 지정한
Pod)를 직접 호출해 특정 Pod만 실패하는지 가린다. 호출 하나의 실패는 결과에 기록하고 계속한다.
"""

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.error.service_exception import ErrorCode, ServiceException
from core.http import DEFAULT_TIMEOUT_S, HttpCallError
from service.tools._common import (
    HttpPath,
    K8sName,
    by_name,
    ensure_in_namespace,
    matches,
    read,
    require,
    service_port,
)
from service.tools.base import Tool, ToolContext, ToolObservation

MAX_PODS = 10  # via=pod로 직접 부르는 Pod 상한
MAX_CALLS = 20  # 도구 호출 한 번의 HTTP 호출 상한 (Pod 수 × times)
DEADLINE_S = 30.0  # 도구 호출 한 번의 전체 시간 상한 (조사 150초 안에 머물기 위함)
_clock = time.monotonic

Obj = dict[str, Any]


class Probe(Tool):
    name = "probe"
    description = (
        "진단 대상 서비스(또는 Pod)에 HTTP GET을 반복해 상태 코드와 응답 시간을 본다. 서비스가 "
        "실제로 응답하는지 확인할 때, via=pod로 Pod마다 직접 불러 특정 Pod만 실패하는지 가릴 때 "
        "쓰세요."
    )
    failure_kind = "probe_result"

    class Input(BaseModel):
        target: K8sName = Field(description="Service 이름 또는 Pod 이름")
        path: HttpPath = Field(description="예: 상태 확인 경로 /health")
        via: Literal["service", "pod"] = "service"
        times: int = Field(default=3, ge=1, le=10)

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        snapshot = read(ctx, "Service", "Pod")
        svc = by_name(require(snapshot, "Service"), args.target)
        pods = require(snapshot, "Pod")
        note = ""
        # 계획: (호출 대상, 포트, Pod 이름) 목록. 실행은 상한·마감 시각을 지키며 한다
        plan: list[tuple[dict[str, str], int, str | None]] = []
        if svc is not None:
            kind = "Service"
            ensure_in_namespace(svc)
            if args.via == "service":
                port = service_port(svc)
                plan = [({"service": args.target}, port, None)] * args.times
            else:
                selector = svc["spec"].get("selector") or {}
                targets = [
                    p for p in pods if selector and matches(p["metadata"].get("labels"), selector)
                ]
                if not targets:
                    note = " (selector가 없음)" if not selector else " (selector에 맞는 Pod 없음)"
                for p in targets[:MAX_PODS]:
                    plan += _pod_plan(p, _pod_port(p, svc), args.times)
        else:
            p = by_name(pods, args.target)
            if p is None:
                raise ServiceException(
                    ErrorCode.NOT_FOUND, f"Service·Pod {args.target}이 대상 네임스페이스에 없음"
                )
            if args.via == "service":
                raise ValueError(f"{args.target}은 Pod다. Pod를 직접 부르려면 via=pod")
            kind = "Pod"
            plan = _pod_plan(p, _pod_port(p, None), args.times)

        calls = _execute(ctx, plan[:MAX_CALLS], args.path)
        truncated = len(calls) < len(plan) or (
            svc is not None and args.via == "pod" and len(targets) > MAX_PODS
        )
        ok = sum(1 for c in calls if _success(c))
        data: Obj = {
            "target": args.target,
            "via": args.via,
            "path": args.path,
            "ok": ok,
            "fail": len(calls) - ok,
            "calls": calls,
        }
        summary = f"{kind} {args.target} {args.path} "
        if args.via == "service":
            data["port"] = plan[0][1]
            summary += f"서비스 경유 {len(calls)}회"
        else:
            per_pod: dict[str, Obj] = {}
            for c in calls:
                counts = per_pod.setdefault(c["pod"], {"ok": 0, "fail": 0})
                counts["ok" if _success(c) else "fail"] += 1
            data["pods"] = per_pod
            summary += f"Pod 직접 {len(per_pod)}개×{args.times}회{note}"
        summary += f": 성공 {ok}·실패 {len(calls) - ok}"
        latencies = [c["latency_ms"] for c in calls if c["latency_ms"] is not None]
        if latencies:
            summary += f", 평균 {round(sum(latencies) / len(latencies))}ms"
        failed_pods = [name for name, n in data.get("pods", {}).items() if n["fail"]]
        if failed_pods:
            summary += f", 실패 Pod: {', '.join(failed_pods)}"
        if truncated:
            summary += f" (호출 상한 {MAX_CALLS}회·{DEADLINE_S:g}초에서 멈춤)"
        return ToolObservation(
            kind="probe_result",
            subject=f"{ctx.namespace}/{kind}/{args.target}",
            source=f"GET {args.path} via={args.via} x{args.times}",
            summary=summary,
            data=data,
            truncated=truncated,
        )


def _success(call: Obj) -> bool:
    return call["status"] is not None and call["status"] < 400


def _pod_plan(pod: Obj, port: int, times: int) -> list[tuple[dict[str, str], int, str | None]]:
    name, ip = pod["metadata"]["name"], pod["status"].get("podIP")
    return [({"pod_ip": ip} if ip else {}, port, name)] * times


def _execute(ctx: ToolContext, plan: list, path: str) -> list[Obj]:
    """계획한 호출을 차례로 한다. 마감 시각(DEADLINE_S)을 넘기면 남은 호출을 하지 않는다."""
    deadline = _clock() + DEADLINE_S
    calls = []
    for target, port, pod in plan:
        left = deadline - _clock()
        if left <= 0:
            break
        call: Obj = {"pod": pod, "port": port} if pod else {}
        if not target:
            calls.append({**call, "status": None, "latency_ms": None, "error": "Pod IP 없음"})
            continue
        try:
            resp = ctx.http.get(
                port=port, path=path, timeout_s=min(DEFAULT_TIMEOUT_S, left), **target
            )
            call.update(status=resp.status, latency_ms=resp.elapsed_ms)
        except HttpCallError as e:
            call.update(status=None, latency_ms=None, error=str(e))
        calls.append(call)
    return calls


def _pod_port(pod: Obj, svc: Obj | None) -> int:
    """Pod 직접 호출 포트: Service targetPort(숫자·이름) → 첫 컨테이너 포트."""
    container_ports = [p for c in pod["spec"].get("containers") or [] for p in c.get("ports") or []]
    target = ((svc or {}).get("spec", {}).get("ports") or [{}])[0].get("targetPort")
    if isinstance(target, int):
        return target
    named = next((p for p in container_ports if target and p.get("name") == target), None)
    if named:
        return named["containerPort"]
    if container_ports:
        return container_ports[0]["containerPort"]
    if svc is not None:
        return service_port(svc)
    raise ValueError(f"Pod {pod['metadata']['name']}의 포트를 알 수 없음")
