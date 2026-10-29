from datetime import UTC, datetime, timedelta

import pytest

from core.db import AlertStore, CheckRunStore, IncidentStore, create_db_engine
from service.model import ClusterSnapshot

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def test_list_runs_latest_first_limit_and_since(engine):
    store = CheckRunStore(engine)
    ids = [store.create("schedule", NOW + timedelta(hours=i)) for i in range(3)]
    store.save_snapshot(
        ids[1], ClusterSnapshot(namespace="dev", collected_at=NOW, resources={}, failed={})
    )
    runs = store.list_runs(limit=2)
    assert [r[0] for r in runs] == [ids[2], ids[1]]
    assert runs[1][2:] == ("schedule", True) and runs[0][3] is False
    since = store.list_runs(since=NOW + timedelta(hours=1))
    assert [r[0] for r in since] == [ids[2], ids[1]]
    until = store.list_runs(until=NOW + timedelta(hours=1))
    assert [r[0] for r in until] == [ids[1], ids[0]]


def test_exists_and_latest_run(engine):
    store = CheckRunStore(engine)
    assert store.latest_run() is None
    assert not store.exists(1)
    run_id = store.create("manual", NOW)
    assert store.exists(run_id)
    assert store.latest_run() == (run_id, NOW)


def test_list_incidents_filters_and_actions(engine):
    runs = CheckRunStore(engine)
    store = IncidentStore(engine)
    r1, r2 = runs.create("schedule", NOW), runs.create("schedule", NOW)
    a = store.open("fp-a", {}, "high", r1, NOW)
    b = store.open("fp-b", {}, "low", r1, NOW + timedelta(hours=2))
    store.resolve(a, r2, NOW + timedelta(hours=1))
    assert [i.id for i in store.list_incidents()] == [b, a]
    assert [i.id for i in store.list_incidents(status="open")] == [b]
    assert [i.id for i in store.list_incidents(status="resolved")] == [a]
    assert [i.id for i in store.list_incidents(since=NOW + timedelta(hours=1))] == [b]
    assert store.actions_for_runs([r1, r2]) == {
        r1: [(a, "opened"), (b, "opened")],
        r2: [(a, "resolved")],
    }
    assert store.actions_for_runs([]) == {}


def test_list_alerts_filters(engine):
    run_id = CheckRunStore(engine).create("schedule", NOW)
    inc = IncidentStore(engine)
    i1, i2 = inc.open("a", {}, None, run_id, NOW), inc.open("b", {}, None, run_id, NOW)
    store = AlertStore(engine)
    kw = {"channel": "C1", "text": "t"}
    x = store.add(kind="opened", sent_at=NOW, incident_id=i1, **kw)
    y = store.add(kind="opened", sent_at=NOW + timedelta(hours=1), incident_id=i2, **kw)
    assert [a.id for a in store.list_alerts()] == [y, x]
    assert [a.id for a in store.list_alerts(incident_id=i1)] == [x]
    assert [a.id for a in store.list_alerts(since=NOW + timedelta(minutes=30))] == [y]
    assert len(store.list_alerts(limit=1)) == 1
