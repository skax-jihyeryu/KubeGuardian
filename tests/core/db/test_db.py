from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from core.db import CheckRunStore, EvidenceStore, SqliteObservationStore, create_db_engine
from service.checks.detectors.base import Observation, ObservationStore
from service.model import ClusterSnapshot, Evidence

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def _evidence(subject="dev/Pod/agent-1", **kw):
    return Evidence(
        kind="resource",
        subject=subject,
        collected_at=NOW,
        source=f"GET /api/v1/namespaces/dev/pods/{subject.rsplit('/', 1)[-1]}",
        data={"status": {"phase": "Running"}},
        **kw,
    )


def _snapshot(pods):
    return ClusterSnapshot(
        namespace="dev",
        collected_at=NOW,
        resources={"Pod": pods},
        failed={"Secret": "403 Forbidden"},
    )


# ── 엔진 ──


def test_engine_uses_wal_mode(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_tables_are_created_once_and_reopen_keeps_data(tmp_path):
    path = tmp_path / "kg.db"
    run_id = CheckRunStore(create_db_engine(path)).create("schedule", NOW)

    assert CheckRunStore(create_db_engine(path)).get_snapshot(run_id) is None  # 다시 열어도 동작


# ── 점검 이력 ──


def test_check_run_snapshot_round_trips(engine):
    runs = CheckRunStore(engine)
    run_id = runs.create("check", NOW)
    snapshot = _snapshot([{"kind": "Pod", "metadata": {"name": "a"}}])

    runs.save_snapshot(run_id, snapshot)

    loaded = runs.get_snapshot(run_id)
    assert loaded == snapshot
    assert loaded.items("Secret") is None  # 실패한 kind도 그대로 남는다


def test_previous_snapshot_is_latest_earlier_run_with_snapshot(engine):
    runs = CheckRunStore(engine)
    first = runs.create("schedule", NOW)
    runs.save_snapshot(first, _snapshot([{"kind": "Pod", "metadata": {"name": "first"}}]))
    second = runs.create("schedule", NOW + timedelta(minutes=5))
    runs.save_snapshot(second, _snapshot([{"kind": "Pod", "metadata": {"name": "second"}}]))
    runs.create("schedule", NOW + timedelta(minutes=10))  # 수집 전에 실패한 점검
    current = runs.create("schedule", NOW + timedelta(minutes=15))

    previous = runs.previous_snapshot(current)

    assert previous.items("Pod")[0]["metadata"]["name"] == "second"
    assert runs.previous_snapshot(first) is None


def test_check_run_rejects_naive_time(engine):
    with pytest.raises(ValueError, match="timezone"):
        CheckRunStore(engine).create("schedule", datetime(2026, 10, 20, 1, 0))


# ── Evidence ──


def test_evidence_ids_are_issued_sequentially(engine):
    store = EvidenceStore(engine)

    assert store.add(_evidence("dev/Pod/a")) == "ev-0001"
    assert store.add(_evidence("dev/Pod/b")) == "ev-0002"


def test_evidence_round_trips_with_issued_id(engine):
    run_id = CheckRunStore(engine).create("check", NOW)
    store = EvidenceStore(engine, check_run_id=run_id)
    original = _evidence()

    ev_id = store.add(original)

    loaded = store.get(ev_id)
    assert loaded == original.model_copy(update={"id": ev_id})
    assert store.check_run_of(ev_id) == run_id


def test_evidence_ids_are_unique_across_stores_and_runs(engine):
    runs = CheckRunStore(engine)
    a = EvidenceStore(engine, check_run_id=runs.create("schedule", NOW))
    b = EvidenceStore(engine, check_run_id=runs.create("schedule", NOW))

    assert [a.add(_evidence()), b.add(_evidence()), a.add(_evidence())] == [
        "ev-0001",
        "ev-0002",
        "ev-0003",
    ]


@pytest.mark.parametrize("ev_id", ["ev-9999", "ev-x", "42", ""])
def test_unknown_evidence_id_returns_none(engine, ev_id):
    assert EvidenceStore(engine).get(ev_id) is None


def test_evidence_that_already_has_an_id_is_rejected(engine):
    with pytest.raises(ValueError, match="id"):
        EvidenceStore(engine).add(_evidence(id="ev-0042"))


def test_evidence_time_is_stored_in_utc(engine):
    store = EvidenceStore(engine)
    kst = NOW.astimezone(KST)

    loaded = store.get(store.add(_evidence().model_copy(update={"collected_at": kst})))

    assert loaded.collected_at == NOW
    assert loaded.collected_at.utcoffset() == timedelta(0)


# ── 관측 기록 ──


def test_sqlite_observation_store_implements_protocol(engine):
    store: ObservationStore = SqliteObservationStore(engine)

    assert store.get("S02", "dev/Service/agent") is None


def test_touch_keeps_first_seen_and_updates_last_seen(engine):
    store = SqliteObservationStore(engine)

    first = store.touch("S02", "dev/Service/agent", NOW)
    later = store.touch("S02", "dev/Service/agent", NOW + timedelta(minutes=5))

    assert first == Observation(first_seen=NOW, last_seen=NOW, values={})
    assert later.first_seen == NOW
    assert later.last_seen == NOW + timedelta(minutes=5)
    assert store.get("S02", "dev/Service/agent") == later


def test_touch_replaces_values_when_given_and_keeps_them_when_none(engine):
    store = SqliteObservationStore(engine)
    store.touch("W01", "dev/Deployment/agent", NOW, {"updatedReplicas": 1})

    kept = store.touch("W01", "dev/Deployment/agent", NOW + timedelta(minutes=5))
    replaced = store.touch(
        "W01", "dev/Deployment/agent", NOW + timedelta(minutes=10), {"availableReplicas": 0}
    )

    assert kept.values == {"updatedReplicas": 1}
    assert replaced.values == {"availableReplicas": 0}


def test_observation_keys_are_rule_and_subject(engine):
    store = SqliteObservationStore(engine)
    store.touch("S02", "dev/Service/agent", NOW)
    store.touch("S02", "dev/Service/gateway", NOW + timedelta(minutes=1))
    store.touch("W01", "dev/Service/agent", NOW + timedelta(minutes=2))

    assert store.get("S02", "dev/Service/agent").first_seen == NOW
    assert store.get("W01", "dev/Service/agent").first_seen == NOW + timedelta(minutes=2)


def test_clear_removes_only_that_record(engine):
    store = SqliteObservationStore(engine)
    store.touch("S02", "dev/Service/agent", NOW)
    store.touch("S02", "dev/Service/gateway", NOW)

    store.clear("S02", "dev/Service/agent")
    store.clear("S02", "dev/Service/missing")  # 없는 기록을 지워도 오류가 아니다

    assert store.get("S02", "dev/Service/agent") is None
    assert store.get("S02", "dev/Service/gateway") is not None


def test_prune_keeps_only_given_subjects_of_that_rule(engine):
    store = SqliteObservationStore(engine)
    for subject in ("a", "b", "c"):
        store.touch("S02", subject, NOW)
    store.touch("W01", "a", NOW)

    store.prune("S02", {"b"})

    assert [store.get("S02", s) is not None for s in ("a", "b", "c")] == [False, True, False]
    assert store.get("W01", "a") is not None

    store.prune("S02", set())
    assert store.get("S02", "b") is None


def test_observation_times_are_utc_aware(engine):
    store = SqliteObservationStore(engine)

    store.touch("S02", "x", NOW.astimezone(KST))

    obs = store.get("S02", "x")
    assert obs.first_seen == NOW
    assert obs.first_seen.utcoffset() == timedelta(0)


def test_touch_rejects_naive_time(engine):
    with pytest.raises(ValueError, match="timezone"):
        SqliteObservationStore(engine).touch("S02", "x", datetime(2026, 10, 20, 1, 0))
