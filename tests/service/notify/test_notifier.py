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
        "target": {"namespace": "dev"},
        "core_paths": {"chat": ["gateway", "agent"]},
        "slack": {
            "alert_target": "#ops",
            "bot_token_secret": "kg-slack-bot",
            "app_token_secret": "kg-slack-app",
            "report_url": "http://kg.local/p/dev",
            "reminders": {
                "critical": {"first_after": "30m", "then_every": "2h", "max": 3},
                "warning": {"first_after": "2h", "max": 1},
            },
        },
    }
)


class FakeSender:
    dry_run = False

    def __init__(self, fail=False):
        self.posts, self.updates, self.fail, self.n = [], [], fail, 0

    def post(self, channel, text, *, thread_ts=None, broadcast=False):
        if self.fail:
            from core.error.service_exception import ErrorCode, ServiceException

            raise ServiceException(ErrorCode.NOTIFY_FAILED, "channel_not_found")
        self.n += 1
        self.posts.append({"channel": channel, "text": text, "thread": thread_ts, "bc": broadcast})
        return f"ts-{self.n}"

    def update(self, channel, ts, text):
        self.updates.append({"channel": channel, "ts": ts, "text": text})


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
        "judgement_basis": "원인 리소스 직접 조회 · 근거 출처 2종(로그, 설정·상태)",
    }
    return Report.model_validate({**base, **kw})


def test_new_incident_sends_first_alert_before_investigation(env):
    engine, sender, check = env

    [c] = check([_sig()], NOW)

    [m1] = sender.posts
    assert m1["channel"] == "#ops" and m1["thread"] is None
    assert "🔴 긴급" in m1["text"] and "dev" in m1["text"] and "원인을 조사하고" in m1["text"]
    assert "http://kg.local/p/dev/checks/1" in m1["text"]
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
    assert "원인: agent Service selector가 Pod 라벨과 다름" in up["text"]
    assert "조치: Service agent의 selector" in up["text"] and "(판단 확정)" in up["text"]
    assert "판단: 확정 (원인 리소스 직접 조회 · 근거 출처 2종(로그, 설정·상태))" in up["text"]
    assert "확신도" not in up["text"]  # LLM 확신도는 알림에 쓰지 않는다
    reply = sender.posts[-1]
    assert reply["thread"] == "ts-1" and reply["text"].startswith("원인을 확인했습니다")


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

    assert sender.updates[0]["text"].startswith(mark)
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
    assert "🟠 주의" in sender.posts[0]["text"]


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
    _, sender, check = env
    check([_sig()], NOW)
    check([_sig()], NOW + timedelta(minutes=31))
    check([_sig(verdict="passed")], NOW + timedelta(minutes=40))

    assert "31분째" in sender.posts[1]["text"] and "약 40분" in sender.posts[2]["text"]


# ── 판단 수준 (#106) ──


@pytest.mark.parametrize(
    ("judgement", "title", "reply"),
    [
        ("candidate", "(판단 후보)", "원인 후보를 찾았습니다"),
        ("guess", "(판단 추정)", "조사를 마쳤지만 원인을 확정하지 못했습니다"),
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
