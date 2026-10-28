import pytest
from slack_sdk.errors import SlackApiError

from core.config import KubeGuardianConfig, Settings
from core.error.service_exception import ErrorCode, ServiceException
from service.notify.sender import DryRunSender, SlackSender, build_sender


class FakeWebClient:
    """slack_sdk WebClient의 chat_postMessage·chat_update만 흉내 낸다."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], list(fail)

    def _maybe_fail(self):
        if self.fail:
            error = self.fail.pop(0)
            if error == "timeout":
                raise TimeoutError("timed out")
            raise SlackApiError("error", {"ok": False, "error": error})

    def chat_postMessage(self, **kw):  # noqa: N802
        self.calls.append(("post", kw))
        self._maybe_fail()
        return {"ok": True, "ts": "1700000000.000100"}

    def chat_update(self, **kw):
        self.calls.append(("update", kw))
        self._maybe_fail()
        return {"ok": True}


def test_parent_message_has_no_thread_fields():
    client = FakeWebClient()

    ts = SlackSender(client).post("#ops", "본문")

    assert ts == "1700000000.000100"
    assert client.calls == [("post", {"channel": "#ops", "text": "본문"})]


def test_thread_reply_can_be_broadcast_to_channel():
    client = FakeWebClient()

    SlackSender(client).post("#ops", "복구", thread_ts="1.1", broadcast=True)

    assert client.calls[0][1] == {
        "channel": "#ops",
        "text": "복구",
        "thread_ts": "1.1",
        "reply_broadcast": True,
    }


def test_update_edits_the_same_message():
    client = FakeWebClient()

    SlackSender(client).update("#ops", "1.1", "원인 갱신")

    assert client.calls == [("update", {"channel": "#ops", "ts": "1.1", "text": "원인 갱신"})]


def test_persistent_error_becomes_notify_failed_with_slack_reason():
    client = FakeWebClient(fail=["channel_not_found"])

    with pytest.raises(ServiceException) as e:
        SlackSender(client).post("#nope", "본문")

    assert e.value.error_code is ErrorCode.NOTIFY_FAILED and e.value.detail == "channel_not_found"


def _config(slack=True):
    data = {"target": {"namespace": "dev"}}
    if slack:
        data["slack"] = {"alert_target": "#ops", "bot_token_secret": "b", "app_token_secret": "a"}
    return KubeGuardianConfig.model_validate(data)


def test_without_token_or_slack_config_sends_nothing():
    assert isinstance(build_sender(Settings(SLACK_BOT_TOKEN=""), _config()), DryRunSender)
    assert isinstance(
        build_sender(Settings(SLACK_BOT_TOKEN="xoxb-x"), _config(False)), DryRunSender
    )
    assert isinstance(build_sender(Settings(SLACK_BOT_TOKEN="xoxb-x"), _config()), SlackSender)


def test_dry_run_gives_distinct_ids_for_threading():
    sender = DryRunSender()
    assert sender.post("#ops", "a") != sender.post("#ops", "b")


def test_post_is_not_resent_after_timeout_to_avoid_duplicates():
    client = FakeWebClient(fail=["timeout"])

    with pytest.raises(ServiceException):
        SlackSender(client).post("#ops", "본문")

    assert len(client.calls) == 1  # 요청이 Slack에 닿았을 수 있어 다시 보내지 않는다


def test_update_is_retried_after_timeout():
    client = FakeWebClient(fail=["timeout"])

    SlackSender(client).update("#ops", "1.1", "갱신")  # 같은 메시지를 다시 고치는 것은 안전하다

    assert len(client.calls) == 2


def test_permanent_error_is_not_retried():
    client = FakeWebClient(fail=["channel_not_found", "channel_not_found"])

    with pytest.raises(ServiceException):
        SlackSender(client).post("#nope", "본문")

    assert len(client.calls) == 1


def test_rate_limit_is_retried_once():
    client = FakeWebClient(fail=["ratelimited"])

    assert SlackSender(client, sleep=lambda s: None).post("#ops", "본문")
    assert len(client.calls) == 2
