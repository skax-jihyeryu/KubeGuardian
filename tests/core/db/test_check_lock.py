import threading
from datetime import UTC, datetime, timedelta

import pytest

from core.db import CheckLockStore, create_db_engine

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TTL = timedelta(seconds=270)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def test_free_lock_is_acquired(engine):
    got = CheckLockStore(engine).acquire("schedule", NOW, TTL)

    assert got.acquired
    assert got.holder.trigger == "schedule"
    assert got.holder.expires_at == NOW + TTL


def test_held_lock_is_not_acquired_and_reports_holder(engine):
    locks = CheckLockStore(engine)
    first = locks.acquire("ui", NOW, TTL)
    locks.attach_run(first.holder.token, 7)

    second = locks.acquire("schedule", NOW + timedelta(seconds=30), TTL)

    assert not second.acquired
    assert second.holder.trigger == "ui"
    assert second.holder.run_id == 7


def test_released_lock_can_be_acquired_again(engine):
    locks = CheckLockStore(engine)
    first = locks.acquire("check", NOW, TTL)
    locks.release(first.holder.token)

    assert locks.acquire("schedule", NOW + timedelta(seconds=1), TTL).acquired


def test_expired_lock_is_taken_over(engine):
    # 보유한 프로세스가 죽어 놓지 못한 잠금은 만료 뒤 다음 점검이 가져간다
    locks = CheckLockStore(engine)
    locks.acquire("schedule", NOW, TTL)

    later = locks.acquire("schedule", NOW + TTL, TTL)

    assert later.acquired
    assert later.holder.acquired_at == NOW + TTL


def test_release_by_old_holder_does_not_free_new_holder(engine):
    locks = CheckLockStore(engine)
    old = locks.acquire("schedule", NOW, TTL)
    new = locks.acquire("ui", NOW + TTL + timedelta(seconds=1), TTL)

    locks.release(old.holder.token)  # 만료 뒤 늦게 끝난 점검

    assert locks.current(NOW + TTL + timedelta(seconds=2)).token == new.holder.token


def test_current_is_none_when_free_or_expired(engine):
    locks = CheckLockStore(engine)
    assert locks.current(NOW) is None

    locks.acquire("check", NOW, TTL)

    assert locks.current(NOW + timedelta(seconds=10)) is not None
    assert locks.current(NOW + TTL) is None


def test_lock_is_shared_across_engines_on_same_file(tmp_path):
    # CronJob과 화면 서버는 다른 프로세스가 같은 SQLite 파일(PVC)을 연다
    path = tmp_path / "kg.db"
    CheckLockStore(create_db_engine(path)).acquire("schedule", NOW, TTL)

    other = CheckLockStore(create_db_engine(path)).acquire("ui", NOW, TTL)

    assert not other.acquired


def test_only_one_of_concurrent_acquirers_wins(tmp_path):
    path = tmp_path / "kg.db"
    create_db_engine(path)
    results, start = [], threading.Barrier(8)

    def contend():
        locks = CheckLockStore(create_db_engine(path))
        start.wait()
        results.append(locks.acquire("check", NOW, TTL).acquired)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1


def test_skip_is_recorded_with_holder(engine):
    locks = CheckLockStore(engine)
    held = locks.acquire("ui", NOW, TTL)
    locks.attach_run(held.holder.token, 3)
    busy = locks.acquire("schedule", NOW + timedelta(seconds=5), TTL)

    locks.record_skip("schedule", NOW + timedelta(seconds=5), busy.holder)

    [skip] = locks.skips()
    assert skip.trigger == "schedule"
    assert skip.skipped_at == NOW + timedelta(seconds=5)
    assert (skip.holder_trigger, skip.holder_run_id) == ("ui", 3)


def test_naive_time_is_rejected(engine):
    with pytest.raises(ValueError, match="timezone-aware"):
        CheckLockStore(engine).acquire("check", datetime(2026, 10, 28, 9, 0), TTL)
