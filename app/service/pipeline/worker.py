"""조사 작업자 (diagnostic-agent, #28, 기획서 §6.6 조사 실행 위치).

주기 점검(CronJob)이 새 이슈·지문 변경을 조사 대기열에 넣으면, 항상 떠 있는 작업자가
몇 초 안에 집어 조사하고 이슈에 결과를 남기며 1차 알림(M1)을 원인으로 갱신한다.
점검 중단 감시(M7)도 같은 주기로 돈다. CronJob 시간 제한(270초)에 조사가 잘리지 않고,
별도 조사 CronJob의 대기 없이 원인 파악 7분을 지킨다.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel
from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import InvestigationQueue
from core.db.investigation_queue import Job
from core.log.logging import get_logging
from service.agent.base import AgentResult
from service.alerts.incidents import record_investigation
from service.model import InvestigationRequest
from service.notify.notifier import Notifier
from service.pipeline.investigate import run_request

logger = get_logging()

# 집은 지 이만큼 지나도 끝나지 않은 조사는 작업자가 죽은 것으로 보고 다시 집는다.
# 조사 상한(150초)에 LLM 지연과 여유를 더했다
STALE_CLAIM = timedelta(minutes=10)


def complete_job(
    engine: Engine, notifier: Notifier, job: Job, result: AgentResult | None, now: datetime
) -> bool:
    """조사를 끝낸다: 아직 내 일이면 알림을 갱신하고 이슈에 결과를 남긴다. 밀려났으면 False.

    알림을 먼저 한다 (기록이 실패해도 운영자는 결과를 받는다).
    """
    if not InvestigationQueue(engine).finish(job):
        logger.info(f"이슈 #{job.incident_id} 조사가 새 요청에 밀려 결과를 남기지 않음")
        return False
    try:
        notifier.after_investigation(job.incident_id, job.check_run_id, result, now)
    except Exception as e:  # noqa: BLE001 — 알림 실패가 조사 결과 기록을 막지 않게
        logger.warning(f"이슈 #{job.incident_id} 알림 실패: {type(e).__name__}")
    record_investigation(engine, job.incident_id, result)
    return True


class Worker:
    def __init__(
        self,
        engine: Engine,
        config: KubeGuardianConfig,
        notifier: Notifier,
        *,
        llm_factory: Callable[[str], BaseChatModel],
        k8s: Any = None,
        http: Any = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._engine = engine
        self._config = config
        self._notifier = notifier
        self._llm_factory = llm_factory
        self._k8s = k8s
        self._http = http
        self._clock = clock
        self._queue = InvestigationQueue(engine)

    def run_once(self) -> int | None:
        """점검 중단을 감시하고, 대기 중인 조사를 하나 처리한다. 처리한 이슈 번호(없으면 None)."""
        try:
            self._notifier.check_stalled(self._clock())
        except Exception as e:  # noqa: BLE001 — 감시 실패가 조사를 막지 않게
            logger.warning(f"점검 중단 감시 실패: {type(e).__name__}")
        job = self._queue.claim(self._clock(), STALE_CLAIM)
        if job is None:
            return None
        result = self._investigate(job)
        complete_job(self._engine, self._notifier, job, result, self._clock())
        return job.incident_id

    def _investigate(self, job: Job) -> AgentResult | None:
        try:
            request = InvestigationRequest.model_validate(job.request)
            return run_request(
                request,
                self._config,
                self._engine,
                check_run_id=job.check_run_id,
                run_id=f"kg-worker-{job.incident_id}-{job.check_run_id}",
                llm_factory=self._llm_factory,
                k8s=self._k8s,
                http=self._http,
            )
        except Exception as e:  # noqa: BLE001 — 한 이슈의 실패가 작업자를 멈추지 않게
            logger.warning(f"이슈 #{job.incident_id} 조사 실패: {type(e).__name__}")
            return AgentResult(status="failed", error=type(e).__name__)
