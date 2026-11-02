"""get_pod_logs: Pod 컨테이너 로그 조회 (상세설계 §3.2 2번).

로그는 수집기 `fetch_logs`로 읽는다 (마스킹한 뒤 grep). 줄 수는 최대 200, 줄 길이도 자른다.
grep은 최근 `MAX_LOG_SCAN`줄에서 찾는다.
"""

import re

from pydantic import BaseModel, Field, field_validator

from core.error.service_exception import ErrorCode, ServiceException
from service.sensors.collector import (
    MAX_LOG_SCAN,
    MAX_LOG_TAIL,
    fetch_logs,
    initial_read_lines,
    parse_grep,
)
from service.tools._common import K8sName, read, require
from service.tools.base import Tool, ToolContext, ToolObservation

MAX_LINE = 500  # 줄 하나의 상한 (스택 트레이스 한 줄짜리 JSON 등)
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


class GetPodLogs(Tool):
    name = "get_pod_logs"
    description = (
        "Pod 컨테이너의 현재 또는 이전(previous=true, 재시작 직전) 로그 끝부분을 본다. "
        "오류 메시지와 발생 시각으로 원인을 확인할 때 쓰세요. "
        f"기본은 로그 끝부분이다. grep은 최근 {MAX_LOG_SCAN}줄에서 키워드가 든 줄을 찾아 "
        "끝 tail줄을 돌려준다. "
        "'|'는 OR(error|timeout)이고 정규식이 아니며 대소문자를 구분하지 않는다. "
        "since_seconds(초)로 최근 기간만 볼 수 있다. 결과에 앞부분 더 있음이 표시되면 "
        "grep이나 since_seconds를 좁혀 다시 부르세요."
    )
    failure_kind = "log_excerpt"

    class Input(BaseModel):
        pod: K8sName
        container: K8sName | None = Field(default=None, description="비우면 첫 번째 컨테이너")
        previous: bool = False
        tail: int = Field(default=100, ge=1, le=MAX_LOG_TAIL)
        grep: str | None = Field(  # 한 줄 요약에 들어가므로 개행은 받지 않는다
            default=None, min_length=1, max_length=100, pattern=r"^[^\r\n]+$"
        )

        since_seconds: int | None = Field(default=None, ge=1, le=86400)

        @field_validator("grep")
        @classmethod
        def _grep_has_keyword(cls, value: str | None) -> str | None:
            if value is not None:
                parse_grep(value)  # 키워드가 없으면 ValueError
            return value

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        container = args.container or _first_container(ctx, args.pod)
        read_result = fetch_logs(
            ctx.k8s,
            args.pod,
            container,
            previous=args.previous,
            tail=args.tail,
            grep=args.grep,
            since_seconds=args.since_seconds,
        )
        lines = read_result.lines
        clipped = [line[:MAX_LINE] + "…" if len(line) > MAX_LINE else line for line in lines]
        times = [m.group(0) for line in lines if (m := _TIME.search(line[:64]))]
        first, last = (times[0], times[-1]) if times else (None, None)

        summary = f"{args.pod}/{container} " + ("이전 컨테이너 " if args.previous else "")
        summary += f"로그 {len(lines)}줄"
        if args.grep:
            summary += f" ('{args.grep}' {read_result.match_count}건)"
        if first:
            summary += f", {first}~{last}"
        if read_result.more_before:
            summary += f", 앞부분 더 있음(읽은 줄 {read_result.scanned})"
        data = {
            "container": container,
            "previous": args.previous,
            "line_count": len(lines),
            "first_time": first,
            "last_time": last,
            "scanned_lines": read_result.scanned,
            "more_before": read_result.more_before,
            "lines": clipped,
        }
        if args.grep:
            data["match_count"] = read_result.match_count
        query = f"container={container}&tailLines={initial_read_lines(args.tail, args.grep)}"
        query += f"&sinceSeconds={args.since_seconds}" if args.since_seconds else ""
        query += "&previous=true" if args.previous else ""
        return ToolObservation(
            kind="log_excerpt",
            subject=f"{ctx.namespace}/Pod/{args.pod}",
            source=f"GET /api/v1/namespaces/{ctx.namespace}/pods/{args.pod}/log?{query}",
            summary=summary,
            data=data,
            truncated=clipped != lines or read_result.more_before,
        )


def _first_container(ctx: ToolContext, pod: str) -> str:
    pods = require(read(ctx, "Pod"), "Pod")
    found = next((p for p in pods if p["metadata"]["name"] == pod), None)
    if found is None or not found["spec"].get("containers"):
        raise ServiceException(ErrorCode.NOT_FOUND, f"Pod {pod}이 대상 네임스페이스에 없음")
    return found["spec"]["containers"][0]["name"]
