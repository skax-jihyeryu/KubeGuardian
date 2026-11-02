"""Slack 메시지 본문 (UI 정의서 §2). 요약과 링크만 담고 전체 보고서는 화면에서 본다."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.db.incidents import Incident
from core.guard.redact import redact_text
from service.alerts.fingerprint import OTHER_FEATURE
from service.model import Report
from service.model.report import JUDGEMENT_LABEL, severity_for

DISPLAY_TZ = ZoneInfo("Asia/Seoul")  # 운영자에게 보이는 시각
_SEVERITY = {
    "critical": ("🔴", "긴급"),
    "warning": ("🟠", "주의"),
    "info": ("⚪", "참고"),
}
# 판단 수준(코드가 정함, #106). LLM 확신도는 기준이 없어 알림에 쓰지 않는다
_JUDGEMENT_HINT = {
    "confirmed": "조치를 바로 실행해도 됩니다",
    "candidate": "조치 전에 보고서의 근거를 확인하세요",
    "guess": "근거가 부족합니다. 직접 확인이 필요합니다",
}
_MARK = {"incomplete": "⚠️ 조사 미완 ", "failed": "⚠️ 조사 실패 "}


def hhmm(at: datetime) -> str:
    return at.astimezone(DISPLAY_TZ).strftime("%H:%M")


def minutes(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


def escape_mrkdwn(text: str) -> str:
    """Slack이 링크·멘션으로 읽는 `&`, `<`, `>`를 막는다. `*`와 `_`는 본문에서 그대로 둔다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_EVIDENCE_ITEM = r"(?:근거\s*:?\s*)?ev-\d+"
_EVIDENCE_REF = re.compile(
    rf"[ \t]*\[\s*{_EVIDENCE_ITEM}(?:\s*[,~]\s*{_EVIDENCE_ITEM})*\s*\]", re.IGNORECASE
)


def strip_evidence_refs(text: str) -> str:
    """`[ev-0109]` 같은 근거 표기는 Slack에서 읽을 수 없어 지운다. 다른 대괄호는 둔다."""
    stripped = _EVIDENCE_REF.sub("", text)
    return re.sub(r"(?m)^[ \t]+", "", stripped) if stripped != text else stripped


def clean(text: str) -> str:
    """본문 값: 마스킹 → 근거 표기 제거 → 이스케이프. 마스킹이 먼저여야 비밀값이 깨지지 않는다."""
    return escape_mrkdwn(strip_evidence_refs(redact_text(text)))


def bold(text: str) -> str:
    """굵은 글씨 안에 넣는 값: 본문 처리에 더해 `*`를 바꾸고 줄바꿈·공백을 한 칸으로 합친다."""
    return " ".join(clean(text).replace("*", "\u2217").split())


def code(text: str) -> str:
    """조치의 공백·백틱을 보존한다. 여러 줄이나 백틱이 있으면 코드 블록으로 감싼다."""
    # 본문의 들여쓰기 정리는 명령·스크립트의 의미를 바꿀 수 있어 적용하지 않는다.
    command = escape_mrkdwn(_EVIDENCE_REF.sub("", redact_text(text)))
    if "`" in command or "\n" in command or "\r" in command:
        return f"```\n{command}\n```"
    return f"`{command}`"


def _head(namespace: str, severity: str | None, title: str, mark: str = "") -> str:
    emoji, label = _SEVERITY.get(severity or "", ("", ""))
    lead = f"{label} · " if label else ""
    return f"{emoji} *{lead}{namespace} — {mark}{bold(title)}*".strip()


def subject(incident: Incident) -> str:
    """이슈를 한 줄로 부른다: 영향 기능 (어느 기능에도 속하지 않으면 서비스)."""
    features = [f for f in incident.features if f != OTHER_FEATURE]
    if features:
        return ", ".join(features) + " 기능"
    return ", ".join(incident.services) or "서비스"


def _detected(namespace, incident, reason, at, url, status_line):
    return "\n".join(
        [
            _head(namespace, incident.first_severity, f"{subject(incident)} 장애"),
            f"• 감지: {hhmm(at)} 점검",
            f"• 사유: {clean(reason)}",
            status_line,
            f"🔗 이슈: {url}/issues/{incident.id}",
        ]
    )


def first_alert(
    namespace: str, incident: Incident, reason: str, run_id: int, at: datetime, url: str
) -> str:
    """M1 1차 감지. AI 조사를 기다리지 않고 보낸다."""
    return _detected(
        namespace,
        incident,
        reason,
        at,
        url,
        "🔍 원인을 조사하고 있습니다. 조사가 끝나면 이 메시지를 갱신합니다.",
    )


def changed_alert(
    namespace: str, incident: Incident, reason: str, run_id: int, at: datetime, url: str
) -> str:
    """M1 갱신: 지문이 바뀌어 같은 이슈에서 다시 조사한다."""
    return _detected(
        namespace,
        incident,
        reason,
        at,
        url,
        "🔍 상황이 바뀌어(새 신호·다른 실패 단계) 원인을 다시 조사하고 있습니다.",
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
    link = f"🔗 보고서: {url}/issues/{incident.id}"
    top = report.top_issue if report else None
    if report is None or top is None:
        why = f" ({clean(error)})" if error else ""
        return "\n".join(
            [
                _head(namespace, incident.first_severity, f"{subject(incident)} 장애", mark),
                f"원인을 확정하지 못했습니다{why}. "
                "규칙 점검·증상 결과는 점검 상세에서 볼 수 있습니다.",
                link,
            ]
        )
    label = JUDGEMENT_LABEL.get(report.judgement or "")
    lines = [_head(namespace, severity_for(top.impact), top.title, mark)]
    if label:
        basis = f" ({clean(report.judgement_basis)})" if report.judgement_basis else ""
        hint = _JUDGEMENT_HINT[report.judgement]
        if status == "incomplete" and report.judgement == "confirmed":
            hint = (
                "인용한 근거로는 확정이지만 조사가 끝나지 않았습니다. 확인 범위를 보고 조치하세요"
            )
        lines += [f"*판단:* {label}{basis}", f"→ {hint}"]
    lines += ["*원인*", clean(report.conclusion), "*영향*", clean(report.impact_scope)]
    if report.actions:
        lines += ["*조치*", f"• {code(report.actions[0])}"]
    others = sorted((i for i in report.issues if i is not top), key=lambda i: i.rank)[:2]
    if others:
        lines.append("*그 밖의 중요 문제*")
        for i in others:
            emoji, sev = _SEVERITY[severity_for(i.impact)]
            lines.append(f"• {emoji} {sev} {clean(i.title)}")
    if status == "incomplete":
        lines.append("⚠️ 조사 상한에 닿아 확인하지 못한 것이 있습니다 (보고서의 확인 범위 참고).")
    lines.append(link)
    return "\n".join(lines)


def _reply(headline: str, detail: str) -> str:
    return f"*{headline}*\n{detail}"


def cause_reply(status: str, report: Report | None) -> str:
    """M9: 메시지 갱신만으로는 알림이 울리지 않아 스레드 답글로 다시 알린다."""
    top = report.top_issue if report else None
    if status == "done" and report and top and report.judgement != "guess":
        title = clean(top.title)
        if report.judgement != "confirmed":  # 후보, 또는 판정되지 않은 보고서(None)는 확정이 아니다
            mark = " (판단 후보)" if report.judgement else ""
            return _reply(
                "원인 후보를 찾았습니다",
                f"{title}{mark}. 조치 전에 위 메시지의 보고서 링크에서 근거를 확인하세요.",
            )
        return _reply(
            "원인을 확인했습니다",
            f"{title} (판단 확정). 위 메시지에 조치와 보고서 링크를 갱신했습니다.",
        )
    if status == "incomplete":
        return _reply(
            "조사 미완",
            "조사가 상한에 닿아 끝났습니다. 확인한 데까지 위 메시지에 갱신했습니다.",
        )
    if status == "done":
        return _reply(
            "원인을 확정하지 못했습니다",
            "조사를 마쳤지만 원인을 확정하지 못했습니다. 확인한 범위는 보고서에 있습니다.",
        )
    return _reply("조사에 실패했습니다", "위 메시지에 규칙·증상 결과와 점검 링크를 남겼습니다.")


def reminder(namespace: str, incident: Incident, at: datetime, url: str) -> str:
    """M2: 미해결이 이어짐. AI를 다시 부르지 않는다. 지속 시간은 M3와 같이 이슈를 연 시각부터."""
    title = f"{subject(incident)} 장애가 {minutes(incident.opened_at, at)}분째 이어지고 있습니다"
    return (
        f"⏰ {_head(namespace, incident.first_severity, title)}\n"
        f"• 기준: {hhmm(at)}\n"
        f"🔗 이슈: {url}/issues/{incident.id}"
    )


def recovery(namespace: str, incident: Incident, at: datetime, url: str) -> str:
    """M3: 해결. 스레드 답글이면서 채널에도 보이게 보낸다."""
    return (
        f"✅ *복구 · {namespace} — {subject(incident)}이 정상으로 돌아왔습니다*\n"
        f"• 확인: {hhmm(at)} 점검\n"
        f"• 장애 지속: 약 {minutes(incident.opened_at, at)}분\n"
        f"🔗 이슈: {url}/issues/{incident.id}"
    )


def stalled(namespace: str, last_run_at: datetime, at: datetime, url: str) -> str:
    """M7: 점검이 멈추면 '이상 없음'으로 오인하므로 따로 알린다."""
    return (
        f"⚠️ *점검 중단 · {namespace} — 점검이 {minutes(last_run_at, at)}분째 실행되지 않았습니다*\n"
        f"• 마지막 점검: {hhmm(last_run_at)}\n"
        f"🔗 점검 이력: {url}/checks"
    )


def connection_test(namespace: str, at: datetime) -> str:
    """kg slack-test: 알림 경로 확인용. 실제 알림과 같은 방식(부모 → 스레드 → 수정)으로 보낸다."""
    return f"🔧 *연결 확인 · {namespace}* ({hhmm(at)}). 이 메시지는 무시해도 됩니다."


def unknown(namespace: str, items: list[str], run_id: int, url: str) -> str:
    """M10: 같은 항목을 연속으로 확인하지 못했을 때 알림."""
    bullets = "\n".join(f"• {clean(item)}" for item in items)
    return (
        f"⚠️ *점검 일부 확인 못 함 · {namespace}*\n"
        f"같은 항목을 연속 3회 확인하지 못했습니다:\n"
        f"{bullets}\n"
        f"🔗 점검 판정: {url}/checks/{run_id}#s-judge"
    )
