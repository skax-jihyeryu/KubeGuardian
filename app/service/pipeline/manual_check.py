"""화면 점검: 공유 잠금·실행 기록을 먼저 확보한 뒤 백그라운드에서 실행한다."""

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.accounts import load_accounts
from core.config import KubeGuardianConfig, get_setting
from core.db import CheckLockStore, CheckResultStore, CheckRunStore
from core.db.tables import CheckRunRow
from core.error.service_exception import ErrorCode, ServiceException
from core.http import NamespacedHttp
from core.k8s import K8sClient
from core.log.logging import get_logging
from service.notify.notifier import Notifier
from service.notify.sender import build_sender
from service.pipeline.check import _acquire_lock, _excluded_rules, _run_locked


class ManualCheckConflict(Exception):
    def __init__(self, message: str, run_id: int | None = None):
        super().__init__(message)
        self.run_id = run_id


def start_manual_check(
    engine: Engine,
    config: KubeGuardianConfig,
    now: datetime,
    submit: Callable[[Callable], None],
    *,
    reader_factory=None,
    http_factory=None,
    sender_factory=None,
) -> int:
    locks = CheckLockStore(engine)
    current = locks.current(now)
    if current:
        raise ManualCheckConflict("점검이 진행 중입니다.", current.run_id)
    try:
        lock = _acquire_lock(locks, "ui", now)
    except ServiceException as exc:
        if exc.error_code != ErrorCode.CHECK_ALREADY_RUNNING:
            raise
        current = locks.current(now)
        raise ManualCheckConflict(
            "점검이 진행 중입니다.", current.run_id if current else None
        ) from exc
    try:
        with Session(engine) as session:
            previous = session.execute(
                select(CheckRunRow.id, CheckRunRow.started_at)
                .where(CheckRunRow.trigger == "ui")
                .order_by(CheckRunRow.id.desc())
                .limit(1)
            ).first()
        if previous and now - previous[1] < timedelta(minutes=1):
            raise ManualCheckConflict("직접 점검은 1분 간격으로 실행할 수 있습니다.", previous[0])
        run_id = CheckRunStore(engine).create("ui", now)
        CheckResultStore(engine).start(run_id, now, sorted(_excluded_rules(engine)))
        locks.attach_run(lock.token, run_id)

        def execute():
            try:
                reader = reader_factory(config) if reader_factory else K8sClient.from_config(config)
                http = (
                    http_factory(config)
                    if http_factory
                    else NamespacedHttp(
                        config.target.namespace,
                        accounts=load_accounts(get_setting().KG_ACCOUNTS_DIR),
                    )
                )
                outcome = _run_locked(
                    reader,
                    config,
                    engine,
                    lock=lock,
                    trigger="ui",
                    now=now,
                    http=http,
                    user_symptom=None,
                    run_id=run_id,
                )
                sender = (
                    sender_factory(config)
                    if sender_factory
                    else build_sender(get_setting(), config)
                )
                # 직접 점검은 해결 알림만 보낸다. 새 이슈·리마인더는 보내지 않는다.
                Notifier(engine, config, sender).after_check(
                    run_id,
                    outcome.gate,
                    [c for c in outcome.incidents if c.action == "resolved"],
                    now,
                )
            except Exception as exc:
                with suppress(Exception):
                    store = CheckResultStore(engine)
                    result = store.get(run_id)
                    if result is not None and result.status == "running":
                        store.finish(run_id, "failed", now, None, [], [], error=type(exc).__name__)
                get_logging().warning(f"직접 점검 실패: {type(exc).__name__}")
            finally:
                locks.release(lock.token)

        submit(execute)
        return run_id
    except BaseException:
        locks.release(lock.token)
        raise
