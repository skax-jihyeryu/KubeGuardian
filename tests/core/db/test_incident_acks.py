from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from core.config import ReminderPolicy, Settings, load_kubeguardian_config
from core.db import IncidentAckStore, IncidentStore, create_db_engine
from core.db.tables import Base

EXAMPLE = Path(__file__).resolve().parents[3] / "kubeguardian.example.yaml"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    IncidentStore(engine).open("fp", {}, "critical", 1, NOW)  # 이슈 1
    return engine


def test_first_ack_is_recorded(engine):
    acks = IncidentAckStore(engine)

    assert acks.ack(1, "U1", "홍길동", NOW) is True
    got = acks.get(1)
    assert (got.incident_id, got.user_id, got.user_name, got.acked_at) == (1, "U1", "홍길동", NOW)


def test_second_ack_keeps_first(engine):
    acks = IncidentAckStore(engine)
    acks.ack(1, "U1", "홍길동", NOW)

    assert acks.ack(1, "U2", "김철수", NOW + timedelta(minutes=5)) is False
    assert acks.get(1).user_id == "U1"
    assert acks.get(1).acked_at == NOW


def test_get_without_ack_is_none(engine):
    assert IncidentAckStore(engine).get(7) is None


def test_table_is_added_to_existing_db_file(tmp_path):
    # 예전 DB 파일(새 테이블 없음)을 열어도 incident_ack가 생긴다
    path = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{path}")
    tables = [t for t in Base.metadata.sorted_tables if t.name != "incident_ack"]
    Base.metadata.create_all(old, tables=tables)
    assert "incident_ack" not in inspect(old).get_table_names()

    engine = create_db_engine(path)

    assert "incident_ack" in inspect(engine).get_table_names()


def test_reminder_policy_max_is_optional():
    policy = ReminderPolicy.model_validate({"first_after": "30m", "then_every": "30m"})

    assert policy.max is None and policy.require_ack is False
    assert ReminderPolicy.model_validate(
        {"first_after": "30m", "then_every": "30m", "require_ack": True}
    ).require_ack


def test_slack_app_token_defaults_empty():
    assert Settings.model_fields["SLACK_APP_TOKEN"].default == ""


def test_example_config_critical_requires_ack():
    reminders = load_kubeguardian_config(EXAMPLE).slack.reminders
    assert reminders["critical"].require_ack and reminders["critical"].then_every == "30m"
    assert reminders["warning"].max == 1
