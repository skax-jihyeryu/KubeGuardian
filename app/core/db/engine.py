"""SQLite 엔진. CronJob과 Deployment가 같은 파일(PVC)을 쓰므로 WAL 모드로 연다."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event

from core.db.tables import Base

_BUSY_TIMEOUT_MS = 5000  # 다른 프로세스가 쓰는 중이면 이만큼 기다린다


def create_db_engine(path: str | Path) -> Engine:
    """DB 파일을 열고(없으면 만들고) 테이블을 만든다."""
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine
