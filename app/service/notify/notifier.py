"""알림 흐름: 점검·조사 결과를 Slack 메시지로 (기획서 §6.6, UI 정의서 §1.2·§1.4).

- 주기 점검이 긴급·주의 새 이슈를 열면 AI 조사를 기다리지 않고 M1(1차 감지)을 보낸다.
- 조사가 끝나면 같은 M1을 원인으로 갱신하고(M1u) 스레드에 M9 답글을 단다. 미완·실패도 알린다.
- 지문이 바뀌면 M1을 "다시 조사 중"으로 갱신한다. 해결되면 스레드 + 채널에 M3를 보낸다.
  직접 점검(화면·CLI)에서 해결돼도 같다.
- 재알림은 점검과 무관하게 작업자가 판단한다 (check_reminders). 열린 이슈의 M1 스레드에 M2를 달고,
  첫 M2는 M1 발송 + first_after, 다음은 마지막 M2 + then_every다. 확인(ack)되면 멈춘다 (#156).
- 점검이 주기 세 배 넘게 돌지 않으면 M7을 한 번 보낸다 (호출은 diagnostic-agent 작업자, #28).
- 보내지 못해도 점검·조사는 계속한다. 모든 메시지는 알림 이력(alert)에 남는다 (dry-run 포함).
"""

import re
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.config import KubeGuardianConfig
from core.db import AlertStore, CheckRunStore, IncidentAckStore, IncidentStore
from core.db.incidents import Incident
from core.db.tables import AlertRow, CheckResultRow
from core.error.service_exception import ServiceException
from core.schedule import stalled_after
from service.agent.base import AgentResult
from service.alerts.incidents import IncidentChange
from service.model import GateResult
from service.notify import messages
from service.notify.parent import reconcile_parent
from service.notify.sender import DRY_RUN_PREFIX, Sender


def parse_duration(text: str) -> timedelta:
    """재알림 정책의 시간 (`30m`, `2h`)."""
    value, unit = int(text[:-1]), text[-1]
    return timedelta(minutes=value) if unit == "m" else timedelta(hours=value)


ALERTED = ("critical", "warning")  # 알림은 긴급·주의만 (잠재 위험·개선 권고는 화면에만)
_DRY_CHANNEL = "(dry-run)"
_MENTIONED = re.compile(r"<@([UW][A-Z0-9]{8,})>")
NO_ACK_REMINDER_CAP = 3  # 확인 수단이 없을 때 require_ack 정책의 리마인드 상한


class Notifier:
    def __init__(
        self,
        engine: Engine,
        config: KubeGuardianConfig,
        sender: Sender,
        ack_enabled: bool = False,
    ):
        self._engine = engine
        self._incidents = IncidentStore(engine)
        self._alerts = AlertStore(engine)
        self._acks = IncidentAckStore(engine)
        self._ack_enabled = ack_enabled  # 확인 버튼을 받을 수 있다 (앱 토큰이 있고 dry-run이 아님)
        self._runs = CheckRunStore(engine)
        self._config = config
        self._sender = sender
        slack = config.slack
        self._channel = slack.alert_target if slack else _DRY_CHANNEL
        self._url = config.web.base_url.rstrip("/") + f"/{config.project.id}"
        self._ns = config.target.namespace
        self.dry_run = sender.dry_run
        self.sent: list[str] = []  # 이번에 보낸(또는 기록한) 메시지 종류, 출력용
        self._last_error: str | None = None

    def after_check(
        self, run_id: int, gate: GateResult, changes: list[IncidentChange], now: datetime
    ) -> None:
        self.check_unknown(run_id, now)
        for change in changes:
            incident = self._incidents.get(change.incident_id)
            if incident is None:
                continue
            if change.action == "opened":
                self._first_alert(incident, gate.reason, run_id, now)
            elif change.action == "changed":
                self._changed(incident, gate.reason, run_id, now)
            elif change.action == "continued":
                if not self._thread(incident):  # 1차 알림이 실패했거나 dry-run이었다 → 다시 보낸다
                    self._first_alert(incident, gate.reason, run_id, now)
            elif change.action == "resolved" and (thread := self._thread(incident)):
                text = messages.recovery(self._ns, incident, now, self._url)
                self._post("M3", incident.id, text, now, thread=thread, broadcast=True)

    def after_investigation(
        self, incident_id: int, run_id: int, result: AgentResult | None, now: datetime
    ) -> None:
        incident = self._incidents.get(incident_id)
        if incident is None or incident.first_severity not in ALERTED:
            return  # 알리지 않는 심각도는 화면에만
        status = result.status if result else "failed"
        report = result.report if result else None
        error = result.error if result else None
        text = messages.cause_update(
            self._ns, incident, status, report, error, run_id, self._url, self._owners(incident)
        )
        thread = self._thread(incident)
        if thread is None:  # 1차 알림이 나가지 못했다 → 결과를 담아 새로 보낸다
            if ts := self._post("M1", incident.id, text, now, blocks=self._blocks(incident, text)):
                self._incidents.set_slack_ts(incident.id, ts)
            return
        self._update("M1u", incident, thread, text, now)
        reply = (
            messages.cause_reply(status, report) + f"\n🔗 이슈: {self._url}/issues/{incident.id}"
        )
        self._post("M9", incident.id, reply, now, thread=thread)

    def connection_test(self, now: datetime) -> str | None:
        """실제 알림처럼 새 메시지, 스레드·채널 답글, 수정을 보낸다. 실패 사유나 None."""
        text = messages.connection_test(self._ns, now)
        ts = self._post("TEST", None, text, now)
        if ts is None:
            return self._last_error
        reply = "스레드 답글 확인 (복구·리마인더 알림과 같은 방식)"
        if self._post("TEST", None, reply, now, thread=ts, broadcast=True) is None:
            return self._last_error
        edited = text + "\n✅ 메시지 수정 확인 (원인 갱신과 같은 방식)"
        error = None
        try:
            self._sender.update(self._channel, ts, edited)
        except ServiceException as e:
            error = e.detail or e.error_code.name
        self._record("TEST", None, edited, now, ts=ts, thread=None, error=error)  # 수정도 이력에
        return error

    def check_reminders(self, now: datetime) -> None:
        """열린 이슈마다 리마인드할 때인지 본다. 점검 실행과 무관하게 작업자가 부른다."""
        for incident in self._incidents.open_incidents():
            if incident.first_severity in ALERTED:
                self._maybe_remind(incident, now)

    def check_stalled(self, now: datetime) -> bool:
        """마지막 점검이 주기 세 배보다 오래됐으면 M7을 한 번 보낸다. 보냈으면 True."""
        last_run = self._runs.latest_started_at()
        if last_run is None or now - last_run < stalled_after(self._config.schedule):
            return False
        sent = self._alerts.last_sent("M7")  # 실패한 M7은 보낸 것으로 치지 않는다
        if sent is not None and sent.sent_at > last_run:
            return False
        self._post("M7", None, messages.stalled(self._ns, last_run, now, self._url), now)
        return True

    def check_unknown(self, run_id: int, now: datetime) -> bool:
        """같은 항목을 연속 세 번 확인하지 못했을 때 한 번 알린다."""

        def unknown(result):
            if result.status != "done":
                return set()
            items = {
                f"{r['rule_id']} · {i.get('subject', '')}"
                for r in result.verdicts
                for i in r.get("items", [])
                if i.get("verdict") == "unknown"
            }
            items.update(
                f"기능 점검 {s.get('scenario', '')}"
                + (f" · {s['failed_step']}" if s.get("failed_step") is not None else "")
                for s in result.symptoms
                if s.get("status") == "unknown"
            )
            return items

        # 각 항목의 현재 연속 구간을 찾는다. 실패한 발송은 구간을 끝내지 않는다.
        counts, starts = {}, {}
        with Session(self._engine) as session:
            rows = session.scalars(
                select(CheckResultRow)
                .where(CheckResultRow.run_id <= run_id)
                .order_by(CheckResultRow.run_id.desc())
            )
            active = None
            for row in rows:
                if active is None:
                    if row.run_id != run_id:
                        return False
                    active = unknown(row)
                active &= unknown(row)
                if not active:
                    break
                for item in active:
                    counts[item] = counts.get(item, 0) + 1
                    starts[item] = row.started_at
        repeated = {item for item, count in counts.items() if count >= 3}
        if not repeated:
            return False
        # 현재 연속 구간에서 성공한 알림만 제외한다. 실패했으면 다음 점검에서 다시 보낸다.
        with Session(self._engine) as session:
            sent = session.scalars(
                select(AlertRow).where(
                    AlertRow.kind == "M10",
                    AlertRow.error.is_(None),
                    AlertRow.sent_at >= min(starts[item] for item in repeated),
                )
            )
            for alert in sent:
                sent_items = {
                    line.removeprefix("• ")
                    for line in alert.text.splitlines()
                    if line.startswith("• ")
                }
                repeated -= {
                    item
                    for item in repeated
                    if messages.clean(item) in sent_items and alert.sent_at >= starts[item]
                }
        if not repeated:
            return False
        text = messages.unknown(self._ns, sorted(repeated), run_id, self._url)
        self._post("M10", None, text, now)
        return True

    # ── 내부 ──

    def _first_alert(self, incident: Incident, reason: str, run_id: int, now: datetime) -> None:
        if incident.first_severity not in ALERTED:
            return
        text = messages.first_alert(
            self._ns, incident, reason, run_id, now, self._url, self._owners(incident)
        )
        ts = self._post("M1", incident.id, text, now, blocks=self._blocks(incident, text))
        if ts:
            self._incidents.set_slack_ts(incident.id, ts)

    def _changed(self, incident: Incident, reason: str, run_id: int, now: datetime) -> None:
        thread = self._thread(incident)
        if thread is None:
            self._first_alert(incident, reason, run_id, now)  # 전에는 알리지 않던 심각도로 바뀜
            return
        text = messages.changed_alert(
            self._ns, incident, reason, run_id, now, self._url, self._owners(incident)
        )
        self._update("M1u", incident, thread, text, now)
        self._call_new_owners(incident, thread, now)

    def _call_new_owners(self, incident: Incident, thread: str, now: datetime) -> None:
        """수정(M1u)으로 붙은 멘션은 Slack이 알리지 않는다. 새 담당자는 스레드 답글로 부른다."""
        called = {
            user
            for a in self._alerts.for_incident(incident.id)
            if a.kind in ("M1", "M1m") and not a.error
            for user in _MENTIONED.findall(a.text)
        }
        new = [o for o in self._owners(incident) if o not in called]
        if new:
            text = "\n".join(["상황이 바뀌어 담당자를 부릅니다.", *messages.mention_line(new)])
            self._post("M1m", incident.id, text, now, thread=thread)

    def _owners(self, incident: Incident) -> list[str]:
        """멘션할 담당자: 정책에 든 1차 심각도면 영향 서비스 담당자의 합집합(순서 유지)."""
        slack = self._config.slack
        if slack is None or incident.first_severity not in slack.mention:
            return []
        users = (u for service in incident.services for u in slack.owners.get(service, []))
        return list(dict.fromkeys(users))

    def _blocks(self, incident: Incident, text: str) -> list[dict[str, Any]] | None:
        """긴급 알림에만 확인 버튼(확인됐으면 확인 표시)을 붙인다. 받을 수 없으면 붙이지 않는다."""
        if not self._ack_enabled or incident.first_severity != "critical":
            return None
        return messages.ack_blocks(text, incident.id, self._acks.get(incident.id))

    def _thread(self, incident: Incident) -> str | None:
        """이 발송기로 이어 쓸 수 있는 M1 ID. dry-run ID는 실제 Slack에서 쓰지 않는다."""
        ts = incident.slack_ts
        if ts and (self._sender.dry_run or not ts.startswith(DRY_RUN_PREFIX)):
            return ts
        return None

    def _maybe_remind(self, incident: Incident, now: datetime) -> None:
        slack = self._config.slack
        policy = slack.reminders.get(incident.first_severity) if slack else None  # type: ignore[arg-type]
        first = self._alerts.first_sent(incident.id, "M1")
        thread = self._thread(incident)
        if policy is None or first is None or thread is None:
            return
        if policy.require_ack and self._acks.get(incident.id) is not None:
            return
        limit = policy.max
        if (
            policy.require_ack and not self._ack_enabled
        ):  # 확인할 수단이 없다 → 끝없이 울리지 않는다
            limit = NO_ACK_REMINDER_CAP if limit is None else min(limit, NO_ACK_REMINDER_CAP)
        sent = self._alerts.count(incident.id, "M2")
        if (limit is not None and sent >= limit) or (sent and policy.then_every is None):
            return
        last = self._alerts.last_sent_for(incident.id, "M2")
        # 밀린 리마인드가 연달아 나가지 않게 다음 시각은 마지막 M2 기준이다
        due = (
            last.sent_at + parse_duration(policy.then_every or "0m")
            if last
            else first.sent_at + parse_duration(policy.first_after)
        )
        if now >= due:
            text = messages.reminder(self._ns, incident, now, self._url)
            self._post("M2", incident.id, text, now, thread=thread)

    def _post(
        self,
        kind: str,
        incident_id: int | None,
        text: str,
        now: datetime,
        *,
        thread: str | None = None,
        broadcast: bool = False,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str | None:
        ts, error = None, None
        extra = {"blocks": blocks} if blocks else {}
        try:
            ts = self._sender.post(
                self._channel, text, thread_ts=thread, broadcast=broadcast, **extra
            )
        except ServiceException as e:
            error = e.detail or e.error_code.name
        self._record(kind, incident_id, text, now, ts=ts, thread=thread, error=error)
        return ts

    def _update(self, kind: str, incident: Incident, ts: str, text: str, now: datetime) -> None:
        error = None
        blocks = self._blocks(incident, text)
        extra = {"blocks": blocks} if blocks else {}
        try:
            self._sender.update(self._channel, ts, text, **extra)
        except ServiceException as e:
            error = e.detail or e.error_code.name
        self._record(kind, incident.id, text, now, ts=ts, thread=None, error=error)
        if error is None and blocks is not None:
            # The persisted acknowledgement survives; another click retries.
            with suppress(ServiceException):
                reconcile_parent(self._engine, self._sender, self._channel, incident.id)

    def _record(
        self,
        kind: str,
        incident_id: int | None,
        text: str,
        now: datetime,
        *,
        ts: str | None,
        thread: str | None,
        error: str | None,
    ) -> None:
        self._last_error = error
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
