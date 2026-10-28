"""점검 잠금: 주기 점검·화면 실행·CLI·Slack 명령을 통틀어 점검은 동시에 1건 (UI 정의서 §6.5).

- 잠금은 check_lock 테이블의 id=1 행이다. 잡기는 "없거나 만료됨이면 내 것으로" 한 문장
  upsert라서 여러 프로세스(CronJob·화면 서버)가 같은 파일을 열어도 하나만 잡는다.
- 잡은 쪽은 token으로만 놓는다. 만료 뒤 다른 점검이 가져간 잠금을 늦게 끝난 점검이 지우지 않는다.
- 보유한 프로세스가 죽어도 만료 시각이 지나면 다음 점검이 가져간다.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.db.tables import CheckLockRow, CheckSkipRow, require_aware
from service.model import Trigger

_LOCK_ID = 1


@dataclass(frozen=True)
class CheckLock:
    token: str
    trigger: str
    run_id: int | None
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Acquire:
    acquired: bool
    holder: CheckLock  # 잡았으면 내 잠금, 못 잡았으면 지금 보유자


@dataclass(frozen=True)
class CheckSkip:
    skipped_at: datetime
    trigger: str
    holder_trigger: str
    holder_run_id: int | None


class CheckLockStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def acquire(self, trigger: Trigger, now: datetime, ttl: timedelta) -> Acquire:
        now = require_aware(now)
        token = secrets.token_hex(16)
        stmt = insert(CheckLockRow).values(
            id=_LOCK_ID,
            token=token,
            trigger=trigger,
            run_id=None,
            acquired_at=now,
            expires_at=now + ttl,
        )
        # 시각은 ISO 문자열이라 문자열이 아니라 julianday로 비교한다 (소수 초 자릿수가 달라도 맞게)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                c: stmt.excluded[c]
                for c in ("token", "trigger", "run_id", "acquired_at", "expires_at")
            },
            where=func.julianday(CheckLockRow.expires_at)
            <= func.julianday(stmt.excluded.acquired_at),
        )
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)
            holder = _to_lock(s.get_one(CheckLockRow, _LOCK_ID))
        return Acquire(acquired=holder.token == token, holder=holder)

    def attach_run(self, token: str, run_id: int) -> None:
        stmt = update(CheckLockRow).where(CheckLockRow.token == token).values(run_id=run_id)
        with Session(self._engine) as s, s.begin():
            s.execute(stmt)

    def release(self, token: str) -> None:
        with Session(self._engine) as s, s.begin():
            s.execute(delete(CheckLockRow).where(CheckLockRow.token == token))

    def current(self, now: datetime) -> CheckLock | None:
        """지금 유효한 잠금. 없거나 만료됐으면 None (화면의 버튼 비활성·진행 중 링크용)."""
        with Session(self._engine) as s:
            row = s.get(CheckLockRow, _LOCK_ID)
            lock = _to_lock(row) if row else None
        return lock if lock and lock.expires_at > require_aware(now) else None

    def record_skip(self, trigger: Trigger, now: datetime, holder: CheckLock) -> None:
        row = CheckSkipRow(
            skipped_at=require_aware(now),
            trigger=trigger,
            holder_trigger=holder.trigger,
            holder_run_id=holder.run_id,
        )
        with Session(self._engine) as s, s.begin():
            s.add(row)

    def skips(self) -> list[CheckSkip]:
        stmt = select(CheckSkipRow).order_by(CheckSkipRow.id)
        with Session(self._engine) as s:
            return [
                CheckSkip(r.skipped_at, r.trigger, r.holder_trigger, r.holder_run_id)
                for r in s.scalars(stmt)
            ]


def _to_lock(row: CheckLockRow) -> CheckLock:
    return CheckLock(row.token, row.trigger, row.run_id, row.acquired_at, row.expires_at)
