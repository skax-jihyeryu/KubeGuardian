import ssl
from datetime import timedelta

import pytest

from core.config import KubeGuardianConfig
from core.db import AlertStore, IncidentAckStore, IncidentStore, create_db_engine
from core.error.service_exception import ErrorCode, ServiceException
from service.notify import ack_listener
from tests.service.checks.detectors.cases import NOW

CHANNEL = "C0123456789"
CONFIG = KubeGuardianConfig.model_validate(
    {
        "project": {"id": "dev"},
        "target": {"namespace": "dev"},
        "slack": {"alert_target": CHANNEL, "report_url": "http://kg.local/dev"},
    }
)


class FakeSender:
    dry_run = False

    def __init__(self, fail=False):
        self.posts, self.updates, self.fail = [], [], fail

    def post(self, channel, text, *, thread_ts=None, broadcast=False, blocks=None):
        if self.fail:
            raise ServiceException(ErrorCode.NOTIFY_FAILED, "ratelimited")
        self.posts.append({"channel": channel, "text": text, "thread": thread_ts})
        return "reply-1"

    def update(self, channel, ts, text, blocks=None):
        if self.fail:
            raise ServiceException(ErrorCode.NOTIFY_FAILED, "ratelimited")
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    incidents, alerts = IncidentStore(engine), AlertStore(engine)
    iid = incidents.open("fp", {}, "critical", 1, NOW)  # 이슈 1, 열림
    incidents.set_slack_ts(iid, "100.1")
    alerts.add(
        kind="M1", channel=CHANNEL, text="🔴 *긴급* 본문", sent_at=NOW, incident_id=iid, ts="100.1"
    )
    return engine


def _payload(value="1", *, channel=CHANNEL, user="U1", action_id="kg_ack", type_="block_actions"):
    return {
        "type": type_,
        "user": {"id": user, "username": "hong", "name": "홍길동"},
        "channel": {"id": channel},
        "actions": [{"action_id": action_id, "value": value}],
    }


def _handle(engine, sender, payload, at=NOW + timedelta(minutes=3)):
    return ack_listener.handle_ack(payload, engine=engine, config=CONFIG, sender=sender, now=at)


def test_first_click_records_updates_parent_replies_and_logs_ack(engine):
    sender = FakeSender()

    assert _handle(engine, sender, _payload()) is True

    ack = IncidentAckStore(engine).get(1)
    assert (ack.user_id, ack.user_name) == ("U1", "홍길동")
    [update] = sender.updates
    assert update["ts"] == "100.1" and update["text"] == "🔴 *긴급* 본문"
    assert update["blocks"][-1]["type"] == "context" and "<@U1>" in str(update["blocks"][-1])
    assert sender.posts == [
        {"channel": CHANNEL, "text": "✅ <@U1> 님이 확인했습니다", "thread": "100.1"}
    ]
    acks = [a for a in AlertStore(engine).for_incident(1) if a.kind == "ACK"]
    assert len(acks) == 1 and acks[0].thread_ts == "100.1" and acks[0].error is None


def test_repeated_or_second_person_click_changes_nothing(engine):
    sender = FakeSender()
    _handle(engine, sender, _payload(user="U1"))

    assert _handle(engine, sender, _payload(user="U2")) is False
    assert _handle(engine, sender, _payload(user="U1")) is False

    assert IncidentAckStore(engine).get(1).user_id == "U1"
    assert len(sender.posts) == 1 and len(sender.updates) == 3


@pytest.mark.parametrize(
    "payload",
    [
        _payload(channel="C9999999999"),  # 다른 채널
        _payload(value="abc"),
        _payload(value="-1"),
        _payload(value="0"),
        _payload(value=""),
        _payload(value="99"),  # 없는 이슈
        _payload(action_id="other"),
        _payload(type_="view_submission"),
        {"type": "block_actions"},
        {},
    ],
)
def test_invalid_clicks_record_nothing_and_do_not_raise(engine, payload):
    sender = FakeSender()

    assert _handle(engine, sender, payload) is False

    assert IncidentAckStore(engine).get(1) is None
    assert sender.posts == [] and sender.updates == []


def test_click_on_resolved_incident_removes_button_only(engine):
    IncidentStore(engine).resolve(1, 2, NOW + timedelta(minutes=1))
    sender = FakeSender()

    assert _handle(engine, sender, _payload()) is False

    assert IncidentAckStore(engine).get(1) is None and sender.posts == []
    [update] = sender.updates
    assert update["ts"] == "100.1" and all(b["type"] != "actions" for b in update["blocks"])


def test_slack_failure_keeps_ack_and_records_error(engine):
    sender = FakeSender(fail=True)

    assert _handle(engine, sender, _payload()) is True

    assert IncidentAckStore(engine).get(1) is not None
    [ack_alert] = [a for a in AlertStore(engine).for_incident(1) if a.kind == "ACK"]
    assert ack_alert.error == "ratelimited"


class FakeClient:
    def __init__(self):
        self.responses = []

    def send_socket_mode_response(self, response):
        self.responses.append(response.envelope_id)


class Req:
    def __init__(self, type_="interactive", payload=None):
        self.type, self.envelope_id, self.payload = type_, "env-1", payload or _payload()


def test_request_is_acknowledged_before_it_is_handled():
    client, order = FakeClient(), []
    client.send_socket_mode_response = lambda r: order.append("response")

    ack_listener.on_request(client, Req(), handle=lambda p: order.append("handle"))

    assert order == ["response", "handle"]


def test_handler_error_does_not_stop_listener():
    client = FakeClient()

    def boom(payload):
        raise RuntimeError("boom")

    ack_listener.on_request(client, Req(), handle=boom)  # 예외가 새지 않는다

    assert client.responses == ["env-1"]


def test_non_interactive_requests_are_acknowledged_but_ignored():
    client, seen = FakeClient(), []

    ack_listener.on_request(client, Req("events_api"), handle=seen.append)

    assert client.responses == ["env-1"] and seen == []


def test_ssl_context_uses_ca_file_when_set(monkeypatch):
    import certifi

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert ack_listener.ssl_context() is None

    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    assert isinstance(ack_listener.ssl_context(), ssl.SSLContext)


def test_duplicate_click_retries_failed_parent_with_first_actor(engine):
    sender = FakeSender(fail=True)
    assert _handle(engine, sender, _payload(user="U1"))
    first = IncidentAckStore(engine).get(1)
    sender.fail = False
    assert not _handle(engine, sender, _payload(user="U2"), NOW + timedelta(hours=1))
    assert IncidentAckStore(engine).get(1) == first
    assert "<@U1>" in str(sender.updates[-1]["blocks"])
    assert sender.posts == []
    assert len([a for a in AlertStore(engine).for_incident(1) if a.kind == "ACK"]) == 1


@pytest.mark.parametrize("first_writer", ["worker", "listener"])
def test_overlapping_parent_writes_preserve_latest_content_and_ack(engine, first_writer):
    from service.notify.notifier import Notifier

    class OverlappingSender(FakeSender):
        hook = None
        displayed = None

        def update(self, channel, ts, text, blocks=None):
            hook, self.hook = self.hook, None
            if hook:
                hook()
            super().update(channel, ts, text, blocks)
            self.displayed = (text, blocks)

    sender = OverlappingSender()
    notifier = Notifier(engine, CONFIG, sender, ack_enabled=True)
    incident = IncidentStore(engine).get(1)

    def worker():
        notifier._update("M1u", incident, "100.1", "fresh investigation", NOW)

    def listener():
        _handle(engine, sender, _payload())

    if first_writer == "worker":
        sender.hook = listener
        worker()
    else:
        sender.hook = worker
        listener()
    text, blocks = sender.displayed
    assert text == "fresh investigation"
    assert blocks[-1]["type"] == "context"
    assert "<@U1>" in str(blocks[-1])
    assert len(sender.posts) == 1
    assert len([a for a in AlertStore(engine).for_incident(1) if a.kind == "ACK"]) == 1
