"""Slack 발송 (기획서 §6.6 발송 방식). 봇 토큰으로 chat.postMessage·chat.update를 부른다.

리마인더·복구는 원 메시지(M1)의 스레드에 달아야 해서 Incoming Webhook은 쓰지 않는다.
토큰이나 slack 설정이 없으면(평가·개발) DryRunSender로 보내지 않고 가짜 ID만 돌려준다.
"""

import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from core.config import KubeGuardianConfig, Settings
from core.error.service_exception import ErrorCode, ServiceException

_TIMEOUT_S = 10
_MAX_RETRY_AFTER_S = 5  # ratelimited의 Retry-After를 이보다 오래 기다리지 않는다
# 다시 보내도 되는 Slack 오류 (서버 쪽 일시 오류). 나머지(channel_not_found 등)는 다시 해도 같다
_TRANSIENT = {
    "ratelimited",
    "internal_error",
    "fatal_error",
    "service_unavailable",
    "request_timeout",
}
DRY_RUN_PREFIX = "dry-"


class Sender(Protocol):
    dry_run: bool

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None, broadcast: bool = False
    ) -> str:
        """메시지를 보내고 ts(메시지 ID)를 돌려준다. 실패하면 NOTIFY_FAILED."""
        ...

    def update(self, channel: str, ts: str, text: str) -> None: ...


class SlackSender:
    dry_run = False

    def __init__(self, client: Any, sleep: Callable[[float], None] = time.sleep):
        self._client = client
        self._sleep = sleep

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None, broadcast: bool = False
    ) -> str:
        # 시간 초과 뒤에는 다시 보내지 않는다: 요청이 이미 닿았으면 같은 메시지가 두 번 올라간다
        resp = self._call(
            "chat_postMessage",
            retry_on_timeout=False,
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            reply_broadcast=broadcast if thread_ts else None,
        )
        return str(resp["ts"])

    def update(self, channel: str, ts: str, text: str) -> None:
        self._call("chat_update", retry_on_timeout=True, channel=channel, ts=ts, text=text)

    def _call(self, method: str, *, retry_on_timeout: bool, **kwargs: Any) -> Any:
        """한 번 부르고, 일시 오류면 한 번 더. 끝내 실패하면 NOTIFY_FAILED(사유)."""
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        for attempt in (1, 2):
            try:
                return getattr(self._client, method)(**kwargs)
            except SlackApiError as e:
                error = str(e.response.get("error", "slack_api_error"))
                if attempt == 2 or error not in _TRANSIENT:
                    raise ServiceException(ErrorCode.NOTIFY_FAILED, error) from e
                self._sleep(_retry_after(e))
            except OSError as e:  # 연결 실패·시간 초과
                if attempt == 2 or not retry_on_timeout:
                    raise ServiceException(ErrorCode.NOTIFY_FAILED, type(e).__name__) from e
        raise AssertionError("도달하지 않는다")


def _retry_after(e: SlackApiError) -> float:
    headers = getattr(e.response, "headers", None) or {}
    try:
        return min(float(headers.get("Retry-After", 1)), _MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        return 1.0


class DryRunSender:
    """보내지 않는다. 이력에는 본문이 남고(dry_run=True) 스레드 연결을 위해 가짜 ts를 준다.

    가짜 ts는 프로세스가 달라도 겹치지 않고, DRY_RUN_PREFIX로 실제 ts와 구분된다.
    """

    dry_run = True

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None, broadcast: bool = False
    ) -> str:
        return f"{DRY_RUN_PREFIX}{uuid.uuid4().hex[:12]}"

    def update(self, channel: str, ts: str, text: str) -> None:
        return None


def build_sender(settings: Settings, config: KubeGuardianConfig) -> Sender:
    if config.slack is None or not settings.SLACK_BOT_TOKEN:
        return DryRunSender()
    return SlackSender(WebClient(token=settings.SLACK_BOT_TOKEN, timeout=_TIMEOUT_S))
