"""규칙 감지 이슈·점검은 AI 조사 대신 '규칙 기반 진단 결과'를 보인다 (#185)."""

from core.db import InvestigationQueue
from service.model import Signal
from tests.api.seed import (
    add_evidence,
    at,
    fail_chat_symptom,
    finish_check,
    link_incident,
    make_run,
    open_incident,
)

WAITING = ("조사 대기", "조사 중", "원인을 조사하고", "AI 조사 중")


def _rule_incident(engine, *, with_symptom=False, two_rules=False):
    """규칙이 장애를 적발한 점검과 그 이슈 (경로 A, 조사 큐에 넣지 않고 rule로 표시)."""
    run = make_run(engine)
    ev = add_evidence(engine, run, kind="detector", subject="dev/Service/agent")
    signals = [
        Signal(
            rule_id="S01",
            verdict="detected",
            subject="dev/Service/agent",
            service="agent",
            severity="critical",
            message="selector에 맞는 Pod 없음",
            values={"selector": {"app": "agnet"}},
            evidence_id=ev,
        )
    ]
    if two_rules:
        signals.append(
            Signal(
                rule_id="S04",
                verdict="detected",
                subject="dev/Service/gateway",
                service="gateway",
                severity="warning",
                message="주소가 localhost",
            )
        )
    finish_check(
        engine,
        run,
        path="A",
        severity="critical",
        signals=signals,
        symptoms=[fail_chat_symptom()] if with_symptom else [],
        reason="적발: S01",
    )
    incident = open_incident(engine, run)
    InvestigationQueue(engine).mark_rule(incident)
    return run, incident


def test_issue_shows_rule_result_instead_of_investigation_status(client, engine):
    run, incident = _rule_incident(engine)

    text = client.get(f"/dev/issues/{incident}").text

    assert "규칙 기반 진단 결과" in text
    assert "S01" in text and "selector에 맞는 Pod 없음" in text
    assert "dev/Service/agent" in text
    assert "Service selector와 대상 Pod의 라벨이 일치하는지 확인하세요." in text
    assert f"/dev/checks/{run}?ev=1" in text
    assert "규칙이 확인한 위치와 사실입니다. 근본 원인과 영향 범위는 확인하지 않았습니다." in text
    assert not any(word in text for word in WAITING)


def test_rule_result_lists_each_rule_separately_and_failed_feature(client, engine):
    _, incident = _rule_incident(engine, with_symptom=True, two_rules=True)

    text = client.get(f"/dev/issues/{incident}").text

    assert text.count("selector에 맞는 Pod 없음") >= 1 and "서비스 주소 설정 오류" in text
    assert "dev/Service/gateway" in text
    assert "함께 확인된 기능 실패" in text and "chat 2단계 실패" in text
    assert "하나의 원인" not in text.replace("묶지", "")  # 여러 규칙을 한 원인으로 묶지 않는다


def test_check_detail_of_rule_detection_shows_rule_result_not_queue(client, engine):
    run, _ = _rule_incident(engine, with_symptom=True)

    text = client.get(f"/dev/checks/{run}").text

    assert "규칙 기반 진단 결과" in text
    assert "조사 안 함" in text and "AI 원인 조사" in text
    assert not any(word in text for word in WAITING)


def test_continued_rule_incident_timeline_does_not_claim_ai_skip(client, engine):
    run, incident = _rule_incident(engine)
    for n in (1, 2):  # 이어짐이 연속이면 한 줄로 묶인다
        again = make_run(engine, start=at(300 * n))
        finish_check(
            engine,
            again,
            path="A",
            severity="critical",
            signals=[
                Signal(
                    rule_id="S01",
                    verdict="detected",
                    subject="dev/Service/agent",
                    severity="critical",
                )
            ],
            reason="적발: S01",
            start=at(300 * n),
        )
        link_incident(engine, incident, again, "continued")

    text = client.get(f"/dev/issues/{incident}").text

    assert "AI 조사 생략" not in text
    assert "같은 문제가 이어짐" in text


def _signal(rule="S01", subject="dev/Service/agent"):
    return Signal(rule_id=rule, verdict="detected", subject=subject, severity="critical")


def _targeted_incident(engine, run, targets):
    from core.db import IncidentStore

    incident = IncidentStore(engine).open(
        "fp-rule",
        {"path": "A", "features": ["chat"], "scenarios": [], "targets": targets},
        "critical",
        run,
        at(0),
    )
    InvestigationQueue(engine).mark_rule(incident)
    return incident


def test_rule_report_uses_latest_rule_check_and_only_this_incidents_targets(client, engine):
    first = make_run(engine)
    finish_check(
        engine,
        first,
        path="A",
        severity="critical",
        signals=[_signal(), _signal("S04", "dev/Service/gateway")],
        reason="적발",
    )
    incident = _targeted_incident(engine, first, [["S01", "dev/Service/agent"]])
    later = make_run(engine, start=at(300))
    finish_check(engine, later, path="C", reason="정상", start=at(300))  # 경로 A가 아닌 최근 점검
    link_incident(engine, incident, later, "seen")

    text = client.get(f"/dev/issues/{incident}").text

    assert f"/dev/checks/{first}" in text and "dev/Service/agent" in text
    assert "dev/Service/gateway" not in text  # 다른 이슈의 적발은 섞지 않는다


def test_rule_issue_ignores_old_ai_report_in_title_and_keeps_timeline_link(client, engine):
    from tests.api.seed import finish_investigation, report, start_investigation

    run = make_run(engine)
    incident = open_incident(engine, run)
    inv = start_investigation(engine, run, incident, [])
    finish_investigation(engine, inv, report(conclusion="옛 AI 결론 문장"))
    second = make_run(engine, start=at(300))
    finish_check(engine, second, path="A", severity="critical", signals=[_signal()], start=at(300))
    link_incident(engine, incident, second, "changed")
    InvestigationQueue(engine).mark_rule(incident)

    detail = client.get(f"/dev/issues/{incident}").text
    listing = client.get("/dev/issues?q=옛 AI 결론").text

    assert "규칙 기반 진단 결과" in detail
    assert f"report={inv}" in detail  # 옛 보고서는 타임라인에서 연다
    assert 'class="irow' not in listing  # 검색어는 옛 AI 결론을 쓰지 않는다


def test_continued_gate_line_for_rule_check_is_rule_wording(client, engine):
    run, incident = _rule_incident(engine)
    again = make_run(engine, start=at(300))
    finish_check(engine, again, path="A", severity="critical", signals=[_signal()], start=at(300))
    link_incident(engine, incident, again, "continued")

    text = client.get(f"/dev/checks/{again}").text

    assert "규칙 기반 진단 결과" in text
    assert "다시 조사하지 않습니다" not in text


def test_timeline_ai_skip_wording_only_for_non_rule_checks(client, engine):
    run = make_run(engine)
    incident = open_incident(engine, run)
    for n in (1, 2):
        again = make_run(engine, start=at(300 * n))
        finish_check(
            engine, again, path="B", severity="critical", reason="기능 실패", start=at(300 * n)
        )
        link_incident(engine, incident, again, "continued")
    InvestigationQueue(engine).mark_rule(incident)  # 이슈는 rule이지만 묶인 점검은 경로 B

    text = client.get(f"/dev/issues/{incident}").text

    assert "같은 문제라 AI 조사 생략" in text
