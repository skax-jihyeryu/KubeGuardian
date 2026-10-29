"""Slack 메시지 본문 (UI 정의서 §2). 요약과 링크만 담고 전체 보고서는 화면에서 본다."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.db.incidents import Incident
from service.alerts.fingerprint import OTHER_FEATURE
from service.model import Report
from service.model.report import severity_for

DISPLAY_TZ = ZoneInfo("Asia/Seoul")  # 운영자에게 보이는 시각
_SEVERITY = {"critical": "🔴 긴급", "warning": "🟠 주의", "info": "⚪ 참고"}
_CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}
_MARK = {"incomplete": "⚠️ 조사 미완 ", "failed": "⚠️ 조사 실패 "}


def hhmm(at: datetime) -> str:
    return at.astimezone(DISPLAY_TZ).strftime("%H:%M")


def minutes(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


def _head(namespace: str, severity: str | None) -> str:
    return f"{_SEVERITY.get(severity or '', '')} [KubeGuardian] {namespace}".strip()


def subject(incident: Incident) -> str:
    """이슈를 한 줄로 부른다: 영향 기능 (어느 기능에도 속하지 않으면 서비스)."""
    features = [f for f in incident.features if f != OTHER_FEATURE]
    if features:
        return ", ".join(features) + " 기능"
    return ", ".join(incident.services) or "서비스"


def first_alert(
    namespace: str, incident: Incident, reason: str, run_id: int, at: datetime, url: str
) -> str:
    """M1 1차 감지. AI 조사를 기다리지 않고 보낸다."""
    return (
        f"{_head(namespace, incident.first_severity)} — {subject(incident)}에 장애가 있습니다\n"
        f"감지: {hhmm(at)} 점검 · {reason}\n"
        "🔍 원인을 조사하고 있습니다. 조사가 끝나면 이 메시지를 갱신합니다.\n"
        f"점검 결과: {url}/checks/{run_id}"
    )


def changed_alert(
    namespace: str, incident: Incident, reason: str, run_id: int, at: datetime, url: str
) -> str:
    """M1 갱신: 지문이 바뀌어 같은 이슈에서 다시 조사한다."""
    return (
        f"{_head(namespace, incident.first_severity)} — {subject(incident)}에 장애가 있습니다\n"
        f"감지: {hhmm(at)} 점검 · {reason}\n"
        "🔍 상황이 바뀌어(새 신호·다른 실패 단계) 원인을 다시 조사하고 있습니다.\n"
        f"점검 결과: {url}/checks/{run_id}"
    )


def cause_update(
    namespace: str,
    incident: Incident,
    status: str,
    report: Report | None,
    error: str | None,
    run_id: int,
    url: str,
) -> str:
    """M1 원인 갱신. 조사 미완·실패면 제목 앞에 표시하고 아는 데까지만 담는다."""
    mark = _MARK.get(status, "")
    link = f"보고서: {url}/checks/{run_id}"
    top = report.top_issue if report else None
    if report is None or top is None:
        why = f" ({error})" if error else ""
        return (
            f"{mark}{_head(namespace, incident.first_severity)} — "
            f"{subject(incident)}에 장애가 있습니다\n"
            f"원인을 확정하지 못했습니다{why}. "
            "규칙 점검·증상 결과는 점검 상세에서 볼 수 있습니다.\n"
            f"{link}"
        )
    lines = [
        f"{mark}{_head(namespace, severity_for(top.impact))} — {top.title} "
        f"(확신도 {_CONFIDENCE.get(report.confidence, report.confidence)})",
        f"원인: {report.conclusion}",
        f"영향: {report.impact_scope}",
    ]
    if report.actions:
        lines.append(f"조치: {report.actions[0]}")
    others = sorted((i for i in report.issues if i is not top), key=lambda i: i.rank)[:2]
    lines += [f"그 밖의 중요 문제: {_SEVERITY[severity_for(i.impact)]} {i.title}" for i in others]
    if status == "incomplete":
        lines.append("조사 상한에 닿아 확인하지 못한 것이 있습니다 (보고서의 확인 범위 참고).")
    lines.append(link)
    return "\n".join(lines)


def cause_reply(status: str, report: Report | None) -> str:
    """M9: 메시지 갱신만으로는 알림이 울리지 않아 스레드 답글로 다시 알린다."""
    top = report.top_issue if report else None
    if status == "done" and report and top:
        confidence = _CONFIDENCE.get(report.confidence, report.confidence)
        return (
            f"원인을 확인했습니다: {top.title} (확신도 {confidence}). "
            "위 메시지에 조치와 보고서 링크를 갱신했습니다."
        )
    if status == "incomplete":
        return "조사가 상한에 닿아 끝났습니다(조사 미완). 확인한 데까지 위 메시지에 갱신했습니다."
    if status == "done":
        return "조사를 마쳤지만 원인을 확정하지 못했습니다. 확인한 범위는 보고서에 있습니다."
    return "조사에 실패했습니다. 위 메시지에 규칙·증상 결과와 점검 링크를 남겼습니다."


def reminder(namespace: str, incident: Incident, at: datetime, url: str) -> str:
    """M2: 미해결이 이어짐. AI를 다시 부르지 않는다. 지속 시간은 M3와 같이 이슈를 연 시각부터."""
    severity = incident.first_severity
    return (
        f"⏰ {_head(namespace, severity)} — {subject(incident)} 장애가 "
        f"{minutes(incident.opened_at, at)}분째 이어지고 있습니다 ({hhmm(at)} 기준)\n"
        f"이슈: {url}/issues/{incident.id}"
    )


def recovery(namespace: str, incident: Incident, at: datetime, url: str) -> str:
    """M3: 해결. 스레드 답글이면서 채널에도 보이게 보낸다."""
    return (
        f"✅ [KubeGuardian] {namespace} — {subject(incident)}이 정상으로 돌아왔습니다 "
        f"({hhmm(at)} 점검, 장애 지속 약 {minutes(incident.opened_at, at)}분)\n"
        f"이슈: {url}/issues/{incident.id}"
    )


def stalled(namespace: str, last_run_at: datetime, at: datetime, url: str) -> str:
    """M7: 점검이 멈추면 '이상 없음'으로 오인하므로 따로 알린다."""
    return (
        f"⚠️ [KubeGuardian] {namespace} — 점검이 {minutes(last_run_at, at)}분째 실행되지 않았습니다 "
        f"(마지막 점검 {hhmm(last_run_at)})\n"
        f"점검 이력: {url}/checks"
    )
