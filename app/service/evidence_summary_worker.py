"""중요한 근거의 AI 요약을 작업자가 뒤에서 미리 만든다 (#176). 조사 스레드와 분리된다."""

import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from core.config import get_setting
from core.db import EvidenceStore
from core.db.evidence import _parse_id
from core.db.evidence_summaries import EvidenceSummaryStore
from service.agent.base import LlmFactory
from service.checks.detectors.catalog import CATALOG
from service.evidence_summary import summarize_evidence
from service.model import Evidence

LOOKBACK = timedelta(hours=24)  # 기존 근거를 소급 생성하지 않는다
PAGE = 200


def is_presummary_target(evidence: Evidence) -> bool:
    """조사 도구 근거는 전부, 실패한 기능 점검·호출 실패·장애 신호 적발만. 형식이 어긋나면 False."""
    data = evidence.data
    if evidence.kind in ("resource", "log_excerpt", "event", "probe_result"):
        return True
    if evidence.kind == "app_api":
        status = data.get("status")
        return (
            data.get("failed_step") is not None
            or bool(data.get("error"))
            or (isinstance(status, int) and not isinstance(status, bool) and status >= 400)
        )
    if evidence.kind == "detector":
        rule_id = data.get("rule_id")
        spec = CATALOG.get(rule_id) if isinstance(rule_id, str) else None
        return spec is not None and spec.grade == "signal" and data.get("verdict") == "detected"
    return False


def next_targets(engine: Engine, now: datetime, limit: int = 20) -> list[str]:
    """요약이 없는 대상 근거 번호를 최신순으로. 형식이 어긋난 행은 건너뛴다."""
    summaries, evidence = EvidenceSummaryStore(engine), EvidenceStore(engine)
    found: list[str] = []
    before = None
    while len(found) < limit:
        page = summaries.pending_evidence_ids(now - LOOKBACK, PAGE, before)
        if not page:
            break
        for ev_id in page:
            try:
                item = evidence.get(ev_id)
                target = item is not None and is_presummary_target(item)
            except Exception as e:  # 한 행이 다른 대상을 막지 않는다
                _log(f"{ev_id} 건너뜀 {type(e).__name__}")
                continue
            if target:
                found.append(ev_id)
                if len(found) == limit:
                    break
        before = _parse_id(page[-1])
    return found


def _log(message: str) -> None:
    print(f"근거 요약: {message}", file=sys.stderr, flush=True)


def run_once(
    engine: Engine,
    *,
    model: str,
    llm_factory: LlmFactory,
    clock: Callable[[], datetime],
) -> str | None:
    """대상 한 건을 요약하고 그 상태(done·failed 등)를 돌려준다. 대상이 없으면 None.
    failed도 다시 부르지 않는다. 한 건이 예외를 내면 형식 이름만 남기고 다음 대상으로 넘어간다."""
    for ev_id in next_targets(engine, clock()):
        started = time.monotonic()
        usage: list[dict | None] = []
        try:
            summary = summarize_evidence(
                engine,
                ev_id,
                model=model,
                llm_factory=llm_factory,
                now=clock(),
                retry=False,
                on_usage=usage.append,
            )
        except Exception as e:
            _log(f"{ev_id} 오류 {type(e).__name__}")
            continue
        meta = usage[0] if usage and usage[0] else {}
        tokens = [meta.get(k) if isinstance(meta.get(k), int) else "-" for k in _USAGE_KEYS]
        _log(
            f"{ev_id} {summary.status} {time.monotonic() - started:.1f}초 "
            f"입력 {tokens[0]} 출력 {tokens[1]}"
        )
        return summary.status
    return None


_USAGE_KEYS = ("input_tokens", "output_tokens")
SUCCESS_WAIT_S = 1  # 호출 사이 간격
FAIL_WAIT_S = 60  # 실패하면 쉬고, 연속 실패마다 두 배로 늘린다
FAIL_WAIT_MAX_S = 900


def summary_loop(
    engine: Engine,
    *,
    model: str,
    llm_factory: LlmFactory,
    clock: Callable[[], datetime],
    poll_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """대상이 없으면 poll_s, 성공 뒤 1초, 실패 뒤 60초부터 두 배(최대 15분) 쉬고 성공하면 초기화."""
    failures = 0
    while True:
        try:
            status = run_once(engine, model=model, llm_factory=llm_factory, clock=clock)
        except Exception as e:  # 스레드가 죽으면 이후 요약이 멈춘다
            _log(f"오류 {type(e).__name__}")
            status = None
        if status is None:
            sleep(poll_s)
        elif status == "failed":
            sleep(min(FAIL_WAIT_S * 2**failures, FAIL_WAIT_MAX_S))
            failures += 1
        else:
            failures = 0
            sleep(SUCCESS_WAIT_S)


def start_summary_thread(
    engine: Engine,
    llm_factory: LlmFactory,
    *,
    poll_s: float,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> threading.Thread:
    """조사와 분리된 데몬 스레드. SIGTERM 종료를 막지 않는다."""
    thread = threading.Thread(
        target=summary_loop,
        args=(engine,),
        kwargs=dict(
            model=get_setting().EVIDENCE_SUMMARY_MODEL,
            llm_factory=llm_factory,
            clock=clock,
            poll_s=poll_s,
            sleep=sleep,
        ),
        name="evidence-summary",
        daemon=True,
    )
    thread.start()
    return thread
