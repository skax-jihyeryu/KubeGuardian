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
    assert lines[3:5] == ["*원인*", "dev/Pod/* 가 실패합니다."]
    assert lines[5:7] == ["*영향*", "채팅 전체"]
    assert lines[7:9] == ["*조치*", "• `selector 수정 &lt;a&gt; [Service]`"]
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
    strip = messages.strip_evidence_refs
    assert strip("실패합니다. [ev-0073, ev-0074]") == "실패합니다."
    assert strip("실패합니다 [근거 ev-0109]") == "실패합니다"
    assert strip("실패 [근거: ev-0109] [EV-2] [ev-1, 근거 ev-2]") == "실패"
    assert strip("실패 [ev-1 , ev-2] [ev-1~ev-3]") == "실패"
    assert strip("앞\n [ev-1] 뒤") == "앞\n뒤"
    assert strip("앞 [ev-1]\n뒤") == "앞\n뒤"
    for keep in ("[Service] 실패", "items[0]", "[ev-abc]"):
        assert strip(keep) == keep


def test_escape_mrkdwn_keeps_asterisk():
    assert messages.escape_mrkdwn("dev/Pod/* <a> & a_b") == "dev/Pod/* &lt;a&gt; &amp; a_b"


def test_action_is_inline_code_and_keeps_command():
    cmd = "kubectl get po -o jsonpath='{.items[*].metadata.name}' `x` <a> & b"
    text = messages.cause_update("dev", incident(), "done", report(actions=[cmd]), None, 1, URL)
    assert (
        "• `kubectl get po -o jsonpath='{.items[*].metadata.name}' 'x' &lt;a&gt; &amp; b`"
        in text.splitlines()
    )


def test_bold_values_replace_asterisk_and_collapse_whitespace():
    title = "  dev/Pod/*  \n 실패 "
    issue = Issue(rank=1, title=title, impact="core_path_broken", confidence="high")
    text = messages.cause_update("dev", incident(), "done", report(issues=[issue]), None, 1, URL)
    assert text.splitlines()[0] == "🔴 *긴급 · dev — dev/Pod/∗ 실패*"


SECRETS = [
    ("password=ab<cdSECRET", "api_key=abc<SECRET", "Authorization: ab&cdSECRET"),
]


def test_secrets_with_escaped_chars_are_masked_everywhere():
    pw, key, auth = SECRETS[0]
    issue = Issue(rank=1, title=pw, impact="core_path_broken", confidence="high")
    text = "\n".join(
        [
            messages.cause_update(
                "dev",
                incident(),
                "done",
                report(issues=[issue], conclusion=auth, actions=[key], judgement_basis=pw),
                None,
                1,
                URL,
            ),
            messages.first_alert("dev", incident(), key, 1, NOW, URL),
            messages.cause_update("dev", incident(), "failed", None, auth, 1, URL),
            messages.unknown("dev", [pw], 3, URL),
        ]
    )
    assert "SECRET" not in text


def test_unknown_without_failed_step_has_no_none():
    text = messages.unknown("dev", ["기능 점검 chat"], 3, URL)
    assert "None" not in text
    assert "• 기능 점검 chat" in text


# ── 확인 버튼 (#156) ──


def _ack(user_id="U123", at_=NOW):
    from core.db.incident_acks import IncidentAck

    return IncidentAck(7, user_id, "홍길동", at_)


def test_unacked_blocks_have_body_and_ack_button():
    blocks = messages.ack_blocks("🔴 *긴급*\n본문", 7, None)

    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "🔴 *긴급*\n본문"}}
    assert blocks[-1] == {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "확인"},
                "style": "primary",
                "action_id": "kg_ack",
                "value": "7",
            }
        ],
    }


def test_acked_blocks_show_who_and_when_without_button():
    blocks = messages.ack_blocks("본문", 7, _ack())

    assert [b["type"] for b in blocks] == ["section", "context"]
    assert f"✅ 확인: <@U123> {messages.hhmm(NOW)}" == blocks[-1]["elements"][0]["text"]


def test_long_body_is_split_by_line_under_section_limit():
    lines = [f"줄 {n} " + "가" * 200 for n in range(40)]  # 한 줄 200자, 전체 약 8000자
    blocks = messages.ack_blocks("\n".join(lines), 7, None)

    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert len(sections) >= 3 and all(len(s) <= 3000 for s in sections)
    assert "\n".join(sections).split("\n") == lines  # 내용은 그대로, 줄 경계에서만 나눈다
    assert len(blocks) < 50


def test_single_line_over_limit_is_cut_and_block_count_stays_under_50():
    blocks = messages.ack_blocks("가" * 200_000, 7, None)

    assert len(blocks) < 50 and blocks[-1]["type"] == "actions"
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
