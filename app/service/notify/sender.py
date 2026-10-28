"""Slack 발송 (기획서 §6.6 발송 방식). 봇 토큰으로 chat.postMessage·chat.update를 부른다.

리마인더·복구는 원 메시지(M1)의 스레드에 달아야 해서 Incoming Webhook은 쓰지 않는다.
토큰이나 slack 설정이 없으면(평가·개발) DryRunSender로 보내지 않고 가짜 ID만 돌려준다.
"""

from itertools import count
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from core.config import KubeGuardianConfig, Settings
from core.error.service_exception import ErrorCode, ServiceException

_ATTEMPTS = 2  # 일시 오류에 1회 재시도
_TIMEOUT_S = 10


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

    def __init__(self, client: Any):
        self._client = client

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None, broadcast: bool = False
    ) -> str:
        resp = self._call(
            "chat_postMessage",
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            reply_broadcast=broadcast if thread_ts else None,
        )
        return str(resp["ts"])

    def update(self, channel: str, ts: str, text: str) -> None:
        self._call("chat_update", channel=channel, ts=ts, text=text)

    def _call(self, method: str, **kwargs: Any) -> Any:
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        error = ""
        for _ in range(_ATTEMPTS):
            try:
                return getattr(self._client, method)(**kwargs)
            except SlackApiError as e:
                error = str(e.response.get("error", "slack_api_error"))
            except OSError as e:  # 연결 실패·시간 초과
                error = type(e).__name__
        raise ServiceException(ErrorCode.NOTIFY_FAILED, error)


class DryRunSender:
    """보내지 않는다. 이력에는 본문이 남고(dry_run=True) 스레드 연결을 위해 가짜 ts를 준다."""

    dry_run = True

    def __init__(self) -> None:
        self._ids = count(1)

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None, broadcast: bool = False
    ) -> str:
        return f"dry-{next(self._ids)}"

    def update(self, channel: str, ts: str, text: str) -> None:
        return None


def build_sender(settings: Settings, config: KubeGuardianConfig) -> Sender:
    if config.slack is None or not settings.SLACK_BOT_TOKEN:
        return DryRunSender()
    return SlackSender(WebClient(token=settings.SLACK_BOT_TOKEN, timeout=_TIMEOUT_S))
