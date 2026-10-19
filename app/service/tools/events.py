"""get_events: Kubernetes 이벤트 조회 (상세설계 §3.2 3번).

대상 네임스페이스의 이벤트를 읽어(메시지는 수집기가 마스킹) 조회 기간 안의 것만 최근 순으로 준다.
"""

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from service.tools._common import MAX_ITEMS, K8sName, clip, read, require
from service.tools.base import Tool, ToolContext, ToolObservation

DEFAULT_MINUTES, MAX_MINUTES = 30, 120


class GetEvents(Tool):
    name = "get_events"
    description = (
        "네임스페이스 또는 특정 리소스(kind·name)의 최근 이벤트(유형, 사유, 메시지, 횟수)를 본다. "
        "이미지 받기 실패, 재시작(BackOff), 스케줄링 실패, probe 실패 같은 원인을 확인할 때 쓰세요."
    )
    failure_kind = "event"

    class Input(BaseModel):
        kind: str | None = Field(default=None, pattern=r"^[A-Z][A-Za-z]{1,62}$")
        name: K8sName | None = None
        since_minutes: int = Field(default=DEFAULT_MINUTES, ge=1, le=MAX_MINUTES)

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        now = ctx.now()
        since = now - timedelta(minutes=args.since_minutes)
        found = []
        for e in require(read(ctx, "Event"), "Event"):
            obj = e.get("involvedObject") or {}
            if args.kind and obj.get("kind") != args.kind:
                continue
            if args.name and obj.get("name") != args.name:
                continue
            at = _time(e)
            if at is not None and at >= since:
                found.append((at, e))
        found.sort(key=lambda pair: pair[0], reverse=True)
        events = [_event(e) for _, e in found[:MAX_ITEMS]]

        warnings = sum(1 for _, e in found if e.get("type") == "Warning")
        reasons = Counter(e.get("reason") or "사유 없음" for _, e in found).most_common(3)
        summary = f"이벤트 {len(found)}건 (Warning {warnings}) 최근 {args.since_minutes}분"
        if reasons:
            summary += ": " + ", ".join(f"{r} {n}" for r, n in sorted(reasons, key=_by_count))
        target = f"{args.kind or 'Event'}/{args.name or '*'}"
        return ToolObservation(
            kind="event",
            subject=f"{ctx.namespace}/{target}",
            source=f"GET /api/v1/namespaces/{ctx.namespace}/events",
            summary=summary,
            data={
                "since": since.isoformat(),
                "until": now.isoformat(),
                "count": len(found),
                "warnings": warnings,
                "events": events,
            },
            truncated=len(found) > MAX_ITEMS,
        )


def _by_count(pair: tuple[Any, int]) -> tuple[int, str]:
    return -pair[1], str(pair[0])


def _time(e: dict[str, Any]) -> datetime | None:
    raw = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event(e: dict[str, Any]) -> dict[str, Any]:
    obj = e.get("involvedObject") or {}
    return {
        "type": e.get("type"),
        "reason": e.get("reason"),
        "object": f"{obj.get('kind')}/{obj.get('name')}",
        "message": clip(e.get("message")),
        "count": e.get("count"),
        "last": e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp"),
    }
