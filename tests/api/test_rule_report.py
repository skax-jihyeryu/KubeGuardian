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
