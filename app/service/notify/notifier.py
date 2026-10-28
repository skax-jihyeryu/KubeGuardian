"""알림 흐름: 점검·조사 결과를 Slack 메시지로 (기획서 §6.6, UI 정의서 §1.2·§1.4).

- 주기 점검이 긴급·주의 새 이슈를 열면 AI 조사를 기다리지 않고 M1(1차 감지)을 보낸다.
- 조사가 끝나면 같은 M1을 원인으로 갱신하고(M1u) 스레드에 M9 답글을 단다. 미완·실패도 알린다.
- 지문이 바뀌면 M1을 "다시 조사 중"으로 갱신한다. 해결되면 스레드 + 채널에 M3를 보낸다.
  직접 점검(화면·CLI)에서 해결돼도 같다.
- 같은 지문이 이어지면(continued) 재알림 정책대로 M1 스레드에 M2만 단다. 기준은 M1 발송 시각.
- 점검이 STALL_AFTER 넘게 돌지 않으면 M7을 한 번 보낸다 (호출은 diagnostic-agent 작업자, #28).
- 보내지 못해도 점검·조사는 계속한다. 모든 메시지는 알림 이력(alert)에 남는다 (dry-run 포함).
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import AlertStore, CheckRunStore, IncidentStore
from core.db.incidents import Incident
from core.error.service_exception import ServiceException
from service.agent.base import AgentResult
from service.alerts.incidents import IncidentChange
from service.model import GateResult
from service.notify import messages
from service.notify.sender import Sender


def parse_duration(text: str) -> timedelta:
    """재알림 정책의 시간 (`30m`, `2h`)."""
    value, unit = int(text[:-1]), text[-1]
    return timedelta(minutes=value) if unit == "m" else timedelta(hours=value)


ALERTED = ("critical", "warning")  # 알림은 긴급·주의만 (잠재 위험·개선 권고는 화면에만)
STALL_AFTER = timedelta(minutes=15)  # 5분 주기 3회를 놓치면 점검 중단
_DRY_CHANNEL = "(dry-run)"


class Notifier:
    def __init__(self, engine: Engine, config: KubeGuardianConfig, sender: Sender):
        self._incidents = IncidentStore(engine)
        self._alerts = AlertStore(engine)
        self._runs = CheckRunStore(engine)
        self._config = config
        self._sender = sender
        slack = config.slack
        self._channel = slack.alert_target if slack else _DRY_CHANNEL
        self._url = (slack.report_url if slack else "http://localhost:8000/p/default").rstrip("/")
        self._ns = config.target.namespace
        self.dry_run = sender.dry_run
        self.sent: list[str] = []  # 이번에 보낸(또는 기록한) 메시지 종류, 출력용

    def after_check(
        self, run_id: int, gate: GateResult, changes: list[IncidentChange], now: datetime
    ) -> None:
        for change in changes:
            incident = self._incidents.get(change.incident_id)
            if incident is None:
                continue
            if change.action == "opened":
                self._first_alert(incident, gate.reason, run_id, now)
            elif change.action == "changed":
                self._changed(incident, gate.reason, run_id, now)
            elif change.action == "continued":
                self._maybe_remind(incident, now)
            elif change.action == "resolved" and incident.slack_ts:
                text = messages.recovery(self._ns, incident, now, self._url)
                self._post("M3", incident.id, text, now, thread=incident.slack_ts, broadcast=True)

    def after_investigation(
        self, incident_id: int, run_id: int, result: AgentResult | None, now: datetime
    ) -> None:
        incident = self._incidents.get(incident_id)
        if incident is None or not incident.slack_ts:
            return  # 1차 알림을 보내지 않은 이슈(참고 등)는 갱신할 메시지가 없다
        status = result.status if result else "failed"
        report = result.report if result else None
        error = result.error if result else None
        text = messages.cause_update(self._ns, incident, status, report, error, run_id, self._url)
        self._update("M1u", incident, text, now)
        reply = messages.cause_reply(status, report)
        self._post("M9", incident.id, reply, now, thread=incident.slack_ts)

    def check_stalled(self, now: datetime) -> bool:
        """마지막 점검이 STALL_AFTER보다 오래됐으면 M7을 한 번 보낸다. 보냈으면 True."""
        last_run = self._runs.latest_started_at()
        if last_run is None or now - last_run < STALL_AFTER:
            return False
        sent = self._alerts.last("M7")
        if sent is not None and sent.sent_at > last_run:
            return False
        self._post("M7", None, messages.stalled(self._ns, last_run, now, self._url), now)
        return True

    # ── 내부 ──

    def _first_alert(self, incident: Incident, reason: str, run_id: int, now: datetime) -> None:
        if incident.first_severity not in ALERTED:
            return
        text = messages.first_alert(self._ns, incident, reason, run_id, now, self._url)
        ts = self._post("M1", incident.id, text, now)
        if ts:
            self._incidents.set_slack_ts(incident.id, ts)

    def _changed(self, incident: Incident, reason: str, run_id: int, now: datetime) -> None:
        if not incident.slack_ts:
            self._first_alert(incident, reason, run_id, now)  # 전에는 알리지 않던 심각도로 바뀜
            return
        text = messages.changed_alert(self._ns, incident, reason, run_id, now, self._url)
        self._update("M1u", incident, text, now)

    def _maybe_remind(self, incident: Incident, now: datetime) -> None:
        slack = self._config.slack
        policy = slack.reminders.get(incident.first_severity) if slack else None  # type: ignore[arg-type]
        first = self._alerts.first_sent(incident.id, "M1")
        if policy is None or first is None or not incident.slack_ts:
            return
        sent = self._alerts.count(incident.id, "M2")
        if sent >= policy.max or (sent and policy.then_every is None):
            return
        due = first.sent_at + parse_duration(policy.first_after)
        if sent:
            due += sent * parse_duration(policy.then_every or "0m")
        if now >= due:
            text = messages.reminder(self._ns, incident, first.sent_at, now, self._url)
            self._post("M2", incident.id, text, now, thread=incident.slack_ts)

    def _post(
        self,
        kind: str,
        incident_id: int | None,
        text: str,
        now: datetime,
        *,
        thread: str | None = None,
        broadcast: bool = False,
    ) -> str | None:
        ts, error = None, None
        try:
            ts = self._sender.post(self._channel, text, thread_ts=thread, broadcast=broadcast)
        except ServiceException as e:
            error = e.detail or e.error_code.name
        self.sent.append(kind if error is None else f"{kind} 실패({error})")
        self._alerts.add(
            kind=kind,
            channel=self._channel,
            text=text,
            sent_at=now,
            incident_id=incident_id,
            ts=ts,
            thread_ts=thread,
            dry_run=self._sender.dry_run,
            error=error,
        )
        return ts

    def _update(self, kind: str, incident: Incident, text: str, now: datetime) -> None:
        error = None
        try:
            self._sender.update(self._channel, incident.slack_ts or "", text)
        except ServiceException as e:
            error = e.detail or e.error_code.name
        self.sent.append(kind if error is None else f"{kind} 실패({error})")
        self._alerts.add(
            kind=kind,
            channel=self._channel,
            text=text,
            sent_at=now,
            incident_id=incident.id,
            ts=incident.slack_ts,
            dry_run=self._sender.dry_run,
            error=error,
        )
