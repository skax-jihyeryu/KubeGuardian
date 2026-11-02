from datetime import timedelta

from core.db.incidents import Incident
from service.model import Issue, Report
from service.notify import messages
from tests.api.seed import at

NOW = at(0)
URL = "http://kg.local/dev"


def incident(severity="critical"):
    return Incident(
        id=7,
        status="open",
        fingerprint="f",
        path="C",
        rule_ids=("S01",),
        failed_steps=(),
        services=("agent",),
        features=("chat",),
        targets=(),
        scenarios=(),
        first_severity=severity,
        final_severity=severity,
        investigation=None,
        opened_at=NOW,
        last_seen_at=NOW,
        resolved_at=None,
    )


def report(**kw):
    values = dict(
        conclusion="dev/Pod/* 가 실패합니다. [ev-0073, ev-0074]",
        confidence="high",
        judgement="confirmed",
        judgement_basis="근거 3종 [근거 ev-0109]",
        impact_scope="채팅 전체",
        actions=["selector 수정 <a> [Service]"],
        issues=[
            Issue(rank=1, title="채팅 실패", impact="core_path_broken", confidence="high"),
            Issue(rank=2, title="보조 문제", impact="partial_users", confidence="medium"),
        ],
    )
    values.update(kw)
    return Report(**values)


def all_messages():
    inc = incident()
    later = NOW + timedelta(minutes=31)
    return [
        messages.first_alert("dev", inc, "적발: S01", 1, NOW, URL),
        messages.changed_alert("dev", inc, "적발: S01", 1, NOW, URL),
        messages.cause_update("dev", inc, "done", report(), None, 1, URL),
        messages.cause_update("dev", inc, "failed", None, "오류", 1, URL),
        messages.cause_reply("done", report()),
        messages.reminder("dev", inc, later, URL),
        messages.recovery("dev", inc, later, URL),
        messages.stalled("dev", NOW, later, URL),
        messages.connection_test("dev", NOW),
        messages.unknown("dev", ["S01 · a"], 3, URL),
    ]


def test_no_message_has_product_prefix():
    assert all("[KubeGuardian]" not in text for text in all_messages())


def test_first_alert_is_itemized():
    assert messages.first_alert("dev", incident(), "적발: S01", 1, NOW, URL).splitlines() == [
        "🔴 *긴급 · dev — chat 기능 장애*",
        f"• 감지: {messages.hhmm(NOW)} 점검",
        "• 사유: 적발: S01",
        "🔍 원인을 조사하고 있습니다. 조사가 끝나면 이 메시지를 갱신합니다.",
        "🔗 이슈: http://kg.local/dev/issues/7",
    ]


def test_cause_update_line_order_and_bold_headings():
    lines = messages.cause_update("dev", incident(), "done", report(), None, 1, URL).splitlines()
    assert lines[0] == "🔴 *긴급 · dev — 채팅 실패*"
    assert lines[1].startswith("*판단:* 확정 (근거 3종)")
    assert lines[2].startswith("→ ")
    assert lines[3:5] == ["*원인*", "dev/Pod/∗ 가 실패합니다."]
    assert lines[5:7] == ["*영향*", "채팅 전체"]
    assert lines[7:9] == ["*조치*", "• selector 수정 &lt;a&gt; [Service]"]
    assert lines[9:11] == ["*그 밖의 중요 문제*", "• 🟠 주의 보조 문제"]
    assert lines[-1] == "🔗 보고서: http://kg.local/dev/issues/7"


def test_incomplete_mark_and_warning_kept():
    text = messages.cause_update("dev", incident(), "incomplete", report(), None, 1, URL)
    assert "*긴급 · dev — ⚠️ 조사 미완 채팅 실패*" in text
    assert "⚠️ 조사 상한에 닿아" in text


def test_cause_update_without_report_keeps_error():
    text = messages.cause_update("dev", incident(), "failed", None, "시간 초과", 1, URL)
    assert "*긴급 · dev — ⚠️ 조사 실패 chat 기능 장애*" in text
    assert "원인을 확정하지 못했습니다 (시간 초과)." in text


def test_cause_reply_bold_first_line():
    first, second = messages.cause_reply("done", report()).splitlines()
    assert first == "*원인을 확인했습니다*"
    assert second.startswith("채팅 실패 (판단 확정).")
    for status in ("incomplete", "failed"):
        assert messages.cause_reply(status, None).startswith("*")


def test_strip_evidence_refs():
    assert messages.strip_evidence_refs("실패합니다. [ev-0073, ev-0074]") == "실패합니다."
    assert messages.strip_evidence_refs("실패합니다 [근거 ev-0109]") == "실패합니다"
    assert messages.strip_evidence_refs("[Service] 실패") == "[Service] 실패"


def test_escape_mrkdwn():
    escaped = messages.escape_mrkdwn("dev/Pod/* <a> & a_b")
    assert "*" not in escaped and "<" not in escaped and ">" not in escaped
    assert escaped == "dev/Pod/∗ &lt;a&gt; &amp; a_b"


def test_unknown_without_failed_step_has_no_none():
    text = messages.unknown("dev", ["기능 점검 chat"], 3, URL)
    assert "None" not in text
    assert "• 기능 점검 chat" in text
