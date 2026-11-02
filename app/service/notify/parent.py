"""Reconcile parent content and acknowledgement after overlapping Slack writes."""

from sqlalchemy import Engine

from core.db import AlertStore, IncidentAckStore, IncidentStore
from service.notify import messages
from service.notify.sender import Sender


def reconcile_parent(engine: Engine, sender: Sender, channel: str, incident_id: int) -> None:
    """Repeat if persisted state changed while Slack was accepting the update.

    No database transaction spans the network call. Both parent writers reconcile
    after their write, so the last overlapping writer restores current DB state.
    """
    alerts, incidents, acks = AlertStore(engine), IncidentStore(engine), IncidentAckStore(engine)

    def state():
        incident = incidents.get(incident_id)
        parent = next(
            (
                a
                for a in reversed(alerts.for_incident(incident_id))
                if a.kind in ("M1", "M1u") and not a.error
            ),
            None,
        )
        if incident is None or not incident.slack_ts or parent is None:
            return None
        return incident.slack_ts, incident.status, parent.id, parent.text, acks.get(incident_id)

    current = state()
    while current is not None:
        ts, status, _, text, ack = current
        blocks = messages.ack_blocks(text, incident_id, ack)
        if status != "open":
            blocks = [b for b in blocks if b["type"] != "actions"]
        sender.update(channel, ts, text, blocks=blocks)
        latest = state()
        if latest == current:
            return
        current = latest
