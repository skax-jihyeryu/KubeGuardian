"""Slack 확인 버튼 수신기 (#156). Socket Mode로 버튼 클릭을 받아 이슈 확인을 기록한다.

diagnostic-agent Pod의 listener 컨테이너(`kg slack-listen`)가 돈다.
처리(`handle_ack`)는 Slack 연결과 분리해 두었다. 처리 중 오류는 로그만 남기고 수신을 계속한다.
"""

import os
import ssl
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import AlertStore, IncidentAckStore, IncidentStore
from core.error.service_exception import ServiceException
from core.log.logging import get_logging
from service.notify import messages
from service.notify.sender import Sender

logger = get_logging()

_TIMEOUT_S = 10
_NAME_MAX = 128


def handle_ack(
    payload: dict[str, Any],
    *,
    engine: Engine,
    config: KubeGuardianConfig,
    sender: Sender,
    now: datetime,
) -> bool:
    """버튼 클릭 한 건을 처리한다. 이슈의 첫 확인으로 기록했으면 True.

    잘못된 값·다른 채널·없는 이슈는 기록하지 않는다. 해결된 이슈의 옛 버튼은 눌러도 기록하지 않고
    메시지에서 버튼만 걷어낸다. 기록한 뒤 Slack 호출이 실패해도 기록은 남는다.
    """
    slack = config.slack
    incident_id = _ack_incident_id(payload, slack.alert_target if slack else None)
    user = payload.get("user")
    user_id = user.get("id") if isinstance(user, dict) else None
    if incident_id is None or slack is None or not isinstance(user_id, str) or not user_id:
        return False
    incidents = IncidentStore(engine)
    incident = incidents.get(incident_id)
    if incident is None or not incident.slack_ts:
        return False
    alerts = AlertStore(engine)
    text = next(
        (
            a.text
            for a in reversed(alerts.for_incident(incident_id))
            if a.kind in ("M1", "M1u") and not a.error
        ),
        None,
    )
    if incident.status != "open":
        if text is not None:
            blocks = [
                b for b in messages.ack_blocks(text, incident_id, None) if b["type"] != "actions"
            ]
            _try(lambda: sender.update(slack.alert_target, incident.slack_ts, text, blocks=blocks))
        return False

    name = str(user.get("name") or user.get("username") or user_id)[:_NAME_MAX]
    acks = IncidentAckStore(engine)
    if not acks.ack(incident_id, user_id, name, now):
        return False
    ack = acks.get(incident_id)
    if text is not None:
        blocks = messages.ack_blocks(text, incident_id, ack)
        _try(lambda: sender.update(slack.alert_target, incident.slack_ts, text, blocks=blocks))
    reply = f"✅ <@{messages.clean(user_id)}> 님이 확인했습니다"
    ts, error = None, None
    try:
        ts = sender.post(slack.alert_target, reply, thread_ts=incident.slack_ts)
    except ServiceException as e:
        error = e.detail or e.error_code.name
        logger.warning(f"이슈 #{incident_id} 확인 답글 실패: {error}")
    alerts.add(
        kind="ACK",
        channel=slack.alert_target,
        text=reply,
        sent_at=now,
        incident_id=incident_id,
        ts=ts,
        thread_ts=incident.slack_ts,
        dry_run=sender.dry_run,
        error=error,
    )
    return True


def _ack_incident_id(payload: dict[str, Any], alert_target: str | None) -> int | None:
    """확인 버튼 클릭이면 이슈 번호. 아니거나 값이 이상하거나 알림 채널이 아니면 None."""
    if payload.get("type") != "block_actions":
        return None
    channel = payload.get("channel")
    if not isinstance(channel, dict) or channel.get("id") != alert_target:
        return None
    for action in payload.get("actions") or []:
        if isinstance(action, dict) and action.get("action_id") == messages.ACK_ACTION_ID:
            value = action.get("value")
            if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
                return int(value)
    return None


def _try(call: Callable[[], None]) -> None:
    try:
        call()
    except ServiceException as e:
        logger.warning(f"확인 표시 갱신 실패: {e.detail or e.error_code.name}")


def on_request(
    client: Any, req: SocketModeRequest, *, handle: Callable[[dict[str, Any]], object]
) -> None:
    """Socket Mode 요청: 처리 전에 먼저 응답한다 (늦으면 Slack이 다시 보낸다). 오류는 삼킨다."""
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type != "interactive":
        return
    try:
        handle(req.payload)
    except Exception as e:  # noqa: BLE001 — 한 건의 오류가 수신을 멈추지 않게
        logger.warning(f"확인 처리 실패: {type(e).__name__}")


def ssl_context() -> ssl.SSLContext | None:
    """사내 SSL 프록시 CA(SSL_CERT_FILE)가 있으면 그것으로 검증한다."""
    cafile = os.environ.get("SSL_CERT_FILE")
    return ssl.create_default_context(cafile=cafile) if cafile else None


def listen(
    app_token: str,
    handle: Callable[[dict[str, Any]], object],
    *,
    wait: Callable[[], None] | None = None,
) -> None:
    """Socket Mode에 연결해 클릭을 받는다. wait가 돌아오면 연결을 닫는다."""
    web = WebClient(timeout=_TIMEOUT_S, ssl=ssl_context())  # 연결 주소는 앱 토큰으로 받는다
    client = SocketModeClient(app_token=app_token, web_client=web)
    client.socket_mode_request_listeners.append(lambda c, r: on_request(c, r, handle=handle))
    client.connect()
    try:
        (wait or wait_forever)()
    finally:
        client.close()


def wait_forever() -> None:
    while True:
        time.sleep(3600)
