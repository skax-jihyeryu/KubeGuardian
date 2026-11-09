from datetime import timedelta

import pytest

from core.config import KubeGuardianConfig
from core.db import AlertStore, IncidentStore, create_db_engine
from service.agent.base import AgentResult
from service.alerts.incidents import track
from service.checks.gate import decide_gate
from service.model import Report, Signal
from service.notify.notifier import Notifier
from tests.service.checks.detectors.cases import NOW

CONFIG = KubeGuardianConfig.model_validate(
    {
        "project": {"id": "dev"},
        "target": {"namespace": "dev"},
        "schedule": "*/5 * * * *",
        "core_paths": {"chat": ["gateway", "agent"]},
        "slack": {
            "alert_target": "C0123456789",
            "bot_token_secret": "kg-slack-bot",
            "app_token_secret": "kg-slack-app",
            "report_url": "http://kg.local/dev",
            "reminders": {
                "critical": {"first_after": "30m", "then_every": "30m", "require_ack": True},
                "warning": {"first_after": "2h", "max": 1},
            },
        },
    }
)


class FakeSender:
    dry_run = False

    def __init__(self, fail=False):
        self.posts, self.updates, self.fail, self.n = [], [], fail, 0

    def post(self, channel, text, *, thread_ts=None, broadcast=False, blocks=None):
        if self.fail:
            from core.error.service_exception import ErrorCode, ServiceException

            raise ServiceException(ErrorCode.NOTIFY_FAILED, "channel_not_found")
        self.n += 1
        self.posts.append(
            {
                "channel": channel,
                "text": text,
                "thread": thread_ts,
                "bc": broadcast,
                "blocks": blocks,
            }
        )
        return f"ts-{self.n}"

    def update(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})


def _sig(rule="S01", service="agent", verdict="detected", severity="critical"):
    return Signal(
        rule_id=rule,
        verdict=verdict,
        subject=f"dev/Service/{service}",
        service=service,
        severity=severity if verdict == "detected" else None,
    )


@pytest.fixture
def env(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sender = FakeSender()
    state = {"n": 0}

    def check(signals, at, trigger="schedule"):
        state["n"] += 1
        gate = decide_gate(signals)
        changes = track(
            engine,
            CONFIG,
            run_id=state["n"],
            trigger=trigger,
            now=at,
            gate=gate,
            signals=signals,
            symptoms=[],
        )
        Notifier(engine, CONFIG, sender).after_check(state["n"], gate, changes, at)
        return changes

    return engine, sender, check


def _report(**kw):
    base = {
        "conclusion": "agent Service selector가 Pod 라벨과 다름",
        "confidence": "high",
        "impact_scope": "채팅 전체",
        "issues": [
            {
                "rank": 1,
                "title": "agent selector 오타",
                "impact": "core_path_broken",
                "confidence": "high",
            }
        ],
        "actions": ["Service agent의 selector를 app=agent로 고친다"],
        "judgement": "confirmed",
        "judgement_basis": (
            "원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태) · 지지 가설 근거 2종"
        ),
    }
    return Report.model_validate({**base, **kw})


def test_new_incident_sends_first_alert_before_investigation(env):
    engine, sender, check = env

    [c] = check([_sig()], NOW)

    [m1] = sender.posts
    assert m1["channel"] == "C0123456789" and m1["thread"] is None
    assert (
        "🔴 *긴급 · dev" in m1["text"] and "dev" in m1["text"] and "원인을 조사하고" in m1["text"]
    )
    assert "http://localhost:8080/dev/issues/1" in m1["text"]
    assert IncidentStore(engine).get(c.incident_id).slack_ts == "ts-1"
    assert [a.kind for a in AlertStore(engine).for_incident(c.incident_id)] == ["M1"]


def test_same_fingerprint_sends_nothing_before_reminder_time(env):
    _, sender, check = env
    check([_sig()], NOW)

    check([_sig()], NOW + timedelta(minutes=5))

    assert len(sender.posts) == 1 and sender.updates == []


def test_investigation_updates_first_alert_and_replies(env):
    engine, sender, check = env
    [c] = check([_sig()], NOW)
    result = AgentResult(status="done", report=_report())

    Notifier(engine, CONFIG, sender).after_investigation(c.incident_id, 1, result, NOW)

    [up] = sender.updates
    assert up["ts"] == "ts-1"
    assert "*원인*\nagent Service selector가 Pod 라벨과 다름" in up["text"]
    assert "*조치*\n• `Service agent의 selector" in up["text"]
    basis = "원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태) · 지지 가설 근거 2종"
    assert f"*판단:* 확정 ({basis})" in up["text"]
    assert "확신도" not in up["text"]  # LLM 확신도는 알림에 쓰지 않는다
    reply = sender.posts[-1]
    assert reply["thread"] == "ts-1" and reply["text"].startswith("*원인을 확인했습니다*")


@pytest.mark.parametrize(
    ("status", "mark"), [("failed", "⚠️ 조사 실패"), ("incomplete", "⚠️ 조사 미완")]
)
def test_failed_or_incomplete_investigation_is_marked(env, status, mark):
    engine, sender, check = env
    [c] = check([_sig()], NOW)
    report = _report() if status == "incomplete" else None

    Notifier(engine, CONFIG, sender).after_investigation(
        c.incident_id, 1, AgentResult(status=status, report=report, error="LLM_CALL_FAILED"), NOW
    )

    assert (
        sender.updates[0]["text"].startswith(f"{mark} ")
        or f"— {mark} " in sender.updates[0]["text"].splitlines()[0]
    )
    assert sender.posts[-1]["thread"] == "ts-1"  # 미완·실패여도 답글로 알린다


def test_resolution_sends_recovery_in_thread_and_channel(env):
    _, sender, check = env
    check([_sig()], NOW)

    check([_sig(verdict="passed")], NOW + timedelta(minutes=20))

    m3 = sender.posts[-1]
    assert (m3["thread"], m3["bc"]) == ("ts-1", True)
    assert "✅" in m3["text"] and "20분" in m3["text"]


def test_direct_check_resolution_also_sends_recovery(env):
    _, sender, check = env
    check([_sig()], NOW)

    check([_sig(verdict="passed")], NOW + timedelta(minutes=3), trigger="ui")

    assert sender.posts[-1]["text"].startswith("✅")


def test_changed_fingerprint_updates_first_alert(env):
    _, sender, check = env
    check([_sig()], NOW)

    check([_sig(), _sig("S04", "gateway")], NOW + timedelta(minutes=5))

    assert len(sender.posts) == 1
    assert "다시 조사" in sender.updates[0]["text"] and sender.updates[0]["ts"] == "ts-1"


def test_warning_alerts_and_info_does_not(env):
    _, sender, check = env
    check([_sig(severity="warning")], NOW)
    assert "🟠 *주의 · dev" in sender.posts[0]["text"]


def test_info_severity_incident_is_not_alerted(tmp_path):
    engine, sender = create_db_engine(tmp_path / "kg.db"), FakeSender()
    sig = _sig(severity="warning")
    gate = decide_gate([sig]).model_copy(update={"severity": "info"})
    changes = track(
        engine,
        CONFIG,
        run_id=1,
        trigger="schedule",
        now=NOW,
        gate=gate,
        signals=[sig],
        symptoms=[],
    )

    Notifier(engine, CONFIG, sender).after_check(1, gate, changes, NOW)

    assert sender.posts == []


def test_send_failure_is_recorded_and_does_not_raise(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sig = _sig()
    gate = decide_gate([sig])
    changes = track(
        engine,
        CONFIG,
        run_id=1,
        trigger="schedule",
        now=NOW,
        gate=gate,
        signals=[sig],
        symptoms=[],
    )

    Notifier(engine, CONFIG, FakeSender(fail=True)).after_check(1, gate, changes, NOW)

    [alert] = AlertStore(engine).for_incident(changes[0].incident_id)
    assert alert.kind == "M1" and alert.ts is None and "channel_not_found" in alert.error


def test_stalled_checks_alert_once(env):
    engine, sender, check = env
    check(
        [_sig(verdict="passed")], NOW
    )  # 정상 점검 1회 (점검 이력은 run_check가 쓰므로 여기선 직접)
    from core.db import CheckRunStore

    CheckRunStore(engine).create("schedule", NOW)
    notifier = Notifier(engine, CONFIG, sender)

    assert not notifier.check_stalled(NOW + timedelta(minutes=14))
    assert notifier.check_stalled(NOW + timedelta(minutes=16))
    assert not notifier.check_stalled(NOW + timedelta(minutes=20))  # 같은 중단은 1회만
    assert sender.posts[-1]["text"].startswith("⚠️") and "16분째" in sender.posts[-1]["text"]

    CheckRunStore(engine).create("schedule", NOW + timedelta(minutes=21))  # 다시 돈다
    assert notifier.check_stalled(NOW + timedelta(minutes=37))  # 새 중단은 다시 알린다


# ── 리뷰 반영 ──


def test_failed_first_alert_is_retried_on_next_check(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sig = _sig()
    for n, (sender, minutes) in enumerate([(FakeSender(fail=True), 0), (FakeSender(), 5)], start=1):
        gate = decide_gate([sig])
        at = NOW + timedelta(minutes=minutes)
        changes = track(
            engine,
            CONFIG,
            run_id=n,
            trigger="schedule",
            now=at,
            gate=gate,
            signals=[sig],
            symptoms=[],
        )
        Notifier(engine, CONFIG, sender).after_check(n, gate, changes, at)

    assert IncidentStore(engine).get(1).slack_ts == "ts-1"
    assert [a.kind for a in AlertStore(engine).for_incident(1)] == ["M1", "M1"]


def test_dry_run_thread_is_not_reused_by_real_sender(tmp_path):
    from service.notify.sender import DryRunSender

    engine = create_db_engine(tmp_path / "kg.db")
    sig = _sig()
    for n, sender in enumerate([DryRunSender(), FakeSender()], start=1):
        gate = decide_gate([sig])
        at = NOW + timedelta(minutes=5 * n)
        changes = track(
            engine,
            CONFIG,
            run_id=n,
            trigger="schedule",
            now=at,
            gate=gate,
            signals=[sig],
            symptoms=[],
        )
        Notifier(engine, CONFIG, sender).after_check(n, gate, changes, at)

    # 토큰을 넣은 뒤 첫 점검에서 실제 M1을 새로 보낸다 (dry-run ID로 스레드를 달지 않는다)
    assert sender.posts[0]["thread"] is None and "원인을 조사하고" in sender.posts[0]["text"]
    assert IncidentStore(engine).get(1).slack_ts == "ts-1"


def test_dry_run_ids_are_unique_across_processes():
    from service.notify.sender import DryRunSender

    assert DryRunSender().post("#ops", "a") != DryRunSender().post("#ops", "a")


def test_failed_stall_alert_is_retried(tmp_path):
    from core.db import CheckRunStore

    engine = create_db_engine(tmp_path / "kg.db")
    CheckRunStore(engine).create("schedule", NOW)

    assert Notifier(engine, CONFIG, FakeSender(fail=True)).check_stalled(
        NOW + timedelta(minutes=16)
    )
    assert Notifier(engine, CONFIG, FakeSender()).check_stalled(NOW + timedelta(minutes=21))


def test_done_without_issues_is_not_reported_as_failure(env):
    engine, sender, check = env
    [c] = check([_sig()], NOW)

    Notifier(engine, CONFIG, sender).after_investigation(
        c.incident_id, 1, AgentResult(status="done", report=_report(issues=[])), NOW
    )

    assert "실패" not in sender.posts[-1]["text"] and "확정하지 못" in sender.posts[-1]["text"]


def test_reminder_and_recovery_measure_outage_from_incident_open(env):
    engine, sender, check = env
    check([_sig()], NOW)
    Notifier(engine, CONFIG, sender).check_reminders(NOW + timedelta(minutes=31))
    check([_sig(verdict="passed")], NOW + timedelta(minutes=40))

    assert "31분째" in sender.posts[1]["text"] and "약 40분" in sender.posts[2]["text"]


# ── 판단 수준 (#106) ──


@pytest.mark.parametrize(
    ("judgement", "title", "reply"),
    [
        ("candidate", "*판단:* 후보", "*원인 후보를 찾았습니다*"),
        ("guess", "*판단:* 추정", "*원인을 확정하지 못했습니다*"),
    ],
)
def test_candidate_and_guess_are_shown_as_such(env, judgement, title, reply):
    engine, sender, check = env
    [c] = check([_sig()], NOW)

    Notifier(engine, CONFIG, sender).after_investigation(
        c.incident_id, 1, AgentResult(status="done", report=_report(judgement=judgement)), NOW
    )

    assert title in sender.updates[0]["text"]
    assert sender.posts[-1]["text"].startswith(reply)


def test_unjudged_report_is_not_announced_as_confirmed(env):
    engine, sender, check = env
    [c] = check([_sig()], NOW)

    Notifier(engine, CONFIG, sender).after_investigation(
        c.incident_id, 1, AgentResult(status="done", report=_report(judgement=None)), NOW
    )

    assert "판단" not in sender.updates[0]["text"].splitlines()[0]
    assert sender.posts[-1]["text"].startswith("*원인 후보를 찾았습니다*")
    assert "확정" not in sender.posts[-1]["text"]


def test_incomplete_confirmed_does_not_say_act_now(env):
    engine, sender, check = env
    [c] = check([_sig()], NOW)

    Notifier(engine, CONFIG, sender).after_investigation(
        c.incident_id, 1, AgentResult(status="incomplete", report=_report()), NOW
    )

    text = sender.updates[0]["text"]
    assert "— ⚠️ 조사 미완 " in text.splitlines()[0] and "조치를 바로 실행해도 됩니다" not in text
    assert "조사가 끝나지 않았습니다" in text


# ── 확인 버튼 (#156) ──


def _ack_check(engine, sender, signals, at, run_id, *, ack_enabled=True):
    gate = decide_gate(signals)
    changes = track(
        engine,
        CONFIG,
        run_id=run_id,
        trigger="schedule",
        now=at,
        gate=gate,
        signals=signals,
        symptoms=[],
    )
    notifier = Notifier(engine, CONFIG, sender, ack_enabled=ack_enabled)
    notifier.after_check(run_id, gate, changes, at)
    return notifier, changes


def _buttons(blocks):
    return [b for b in blocks or [] if b["type"] == "actions"]


def test_critical_first_alert_has_ack_button_and_unchanged_text(env):
    engine, sender, _ = env

    _ack_check(engine, sender, [_sig()], NOW, 1)

    [m1] = sender.posts
    assert _buttons(m1["blocks"])[0]["elements"][0]["value"] == "1"
    assert m1["blocks"][0]["text"]["text"] == m1["text"]  # 알림 이력·미리보기용 text는 그대로


def test_warning_has_no_blocks(env):
    engine, sender, _ = env

    _ack_check(engine, sender, [_sig(severity="warning")], NOW, 1)

    assert [p["blocks"] for p in sender.posts] == [None]


def test_disabled_ack_has_no_blocks(env):
    engine, sender, _ = env

    _ack_check(engine, sender, [_sig()], NOW, 1, ack_enabled=False)

    assert [p["blocks"] for p in sender.posts] == [None]


def test_investigation_update_keeps_button_until_acked_then_shows_ack(env):
    from core.db import IncidentAckStore

    engine, sender, _ = env
    notifier, [c] = _ack_check(engine, sender, [_sig()], NOW, 1)
    result = AgentResult(status="done", report=_report())

    notifier.after_investigation(c.incident_id, 1, result, NOW)
    assert _buttons(sender.updates[-1]["blocks"])  # M1u에서도 버튼이 남는다

    IncidentAckStore(engine).ack(c.incident_id, "U9", "홍길동", NOW + timedelta(minutes=3))
    notifier.after_investigation(c.incident_id, 1, result, NOW + timedelta(minutes=4))
    last = sender.updates[-1]["blocks"]
    assert not _buttons(last) and "<@U9>" in last[-1]["elements"][0]["text"]


def test_changed_fingerprint_update_keeps_button(env):
    engine, sender, _ = env
    _ack_check(engine, sender, [_sig()], NOW, 1)

    _ack_check(engine, sender, [_sig(), _sig(rule="S02", service="gateway")], NOW, 2)

    assert _buttons(sender.updates[-1]["blocks"])


def _owner_env(tmp_path, mention=None, owners=None):
    slack = {
        "alert_target": "C0123456789",
        "owners": owners if owners is not None else {"agent": ["U0123456789"]},
        "reminders": {"critical": {"first_after": "30m", "then_every": "30m"}},
    }
    if mention is not None:
        slack["mention"] = mention
    config = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}, "slack": slack})
    engine = create_db_engine(tmp_path / "kg.db")
    sender = FakeSender()
    state = {"n": 0}

    def check(signals, at):
        state["n"] += 1
        gate = decide_gate(signals)
        changes = track(
            engine,
            config,
            run_id=state["n"],
            trigger="schedule",
            now=at,
            gate=gate,
            signals=signals,
            symptoms=[],
        )
        Notifier(engine, config, sender).after_check(state["n"], gate, changes, at)
        return changes

    return engine, sender, check, config


MENTION = "👤 담당: <@U0123456789>"


def test_critical_first_alert_mentions_owner(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path)
    check([_sig()], NOW)
    assert MENTION in sender.posts[0]["text"]


def test_warning_is_not_mentioned_by_default(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path)
    check([_sig(severity="warning")], NOW)
    assert sender.posts and "담당" not in sender.posts[0]["text"]


def test_warning_is_mentioned_when_configured(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path, mention=["critical", "warning"])
    check([_sig(severity="warning")], NOW)
    assert MENTION in sender.posts[0]["text"]


def test_service_without_owner_has_no_mention(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path, owners={"other": ["U0123456789"]})
    check([_sig()], NOW)
    assert "담당" not in sender.posts[0]["text"]


def test_reminder_and_recovery_do_not_mention(tmp_path):
    engine, sender, check, config = _owner_env(tmp_path)
    check([_sig()], NOW)
    Notifier(engine, config, sender).check_reminders(NOW + timedelta(minutes=31))
    check([_sig(verdict="passed")], NOW + timedelta(minutes=35))
    replies = [p for p in sender.posts if p["thread"]]
    assert [p["text"][0] for p in replies] == ["⏰", "✅"]
    assert all("담당" not in p["text"] for p in replies)


def test_investigation_update_keeps_mention_without_new_ping(tmp_path):
    engine, sender, check, config = _owner_env(tmp_path)
    [c] = check([_sig()], NOW)
    Notifier(engine, config, sender).after_investigation(
        c.incident_id, 1, AgentResult(status="done", report=_report()), NOW
    )
    assert MENTION in sender.updates[-1]["text"]
    assert all("담당" not in p["text"] for p in sender.posts[1:])


def test_changed_alert_update_keeps_mention(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path)
    check([_sig()], NOW)
    check([_sig(), _sig(rule="S02")], NOW + timedelta(minutes=5))
    assert sender.updates and MENTION in sender.updates[-1]["text"]


def test_escalation_to_critical_calls_owner_in_thread_reply(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path, mention=["critical"])
    check([_sig(severity="warning")], NOW)
    check([_sig(), _sig(rule="S02")], NOW + timedelta(minutes=5))  # 지문이 바뀌어야 갱신된다
    [reply] = [p for p in sender.posts if p["thread"]]
    assert "<@U0123456789>" in reply["text"]


def test_joined_service_calls_only_new_owner(tmp_path):
    _, sender, check, _ = _owner_env(
        tmp_path, owners={"agent": ["U0123456789"], "db": ["U9876543210"]}
    )
    check([_sig()], NOW)
    check([_sig(), _sig(rule="S02", service="db")], NOW + timedelta(minutes=5))
    [reply] = [p for p in sender.posts if p["thread"]]
    assert "<@U9876543210>" in reply["text"] and "<@U0123456789>" not in reply["text"]


def test_no_new_owner_sends_no_reply(tmp_path):
    _, sender, check, _ = _owner_env(tmp_path)
    check([_sig()], NOW)
    check([_sig(), _sig(rule="S02")], NOW + timedelta(minutes=5))
    assert not [p for p in sender.posts if p["thread"]]


def test_multiple_services_union_deduped(tmp_path):
    _, sender, check, _ = _owner_env(
        tmp_path, owners={"agent": ["U0123456789"], "db": ["U0123456789", "U9876543210"]}
    )
    check([_sig(service="agent"), _sig(rule="S02", service="db")], NOW)
    assert "👤 담당: <@U0123456789> <@U9876543210>" in sender.posts[0]["text"]


def test_no_owners_configured_matches_legacy_text(env):
    _, sender, check = env
    check([_sig()], NOW)
    assert "담당" not in sender.posts[0]["text"]
