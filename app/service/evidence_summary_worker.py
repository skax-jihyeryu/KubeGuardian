"""중요한 근거의 AI 요약을 작업자가 뒤에서 미리 만든다 (#176). 조사 스레드와 분리된다."""

from datetime import datetime, timedelta

from sqlalchemy import Engine

from core.db import EvidenceStore
from core.db.evidence import _parse_id
from core.db.evidence_summaries import EvidenceSummaryStore
from service.checks.detectors.catalog import CATALOG
from service.model import Evidence

LOOKBACK = timedelta(hours=24)  # 기존 근거를 소급 생성하지 않는다
PAGE = 200


def is_presummary_target(evidence: Evidence) -> bool:
    """조사 도구 근거는 전부, 기능 점검·호출 실패·장애 신호 적발만. 형식이 어긋나면 False."""
    data = evidence.data
    if evidence.kind in ("resource", "log_excerpt", "event", "probe_result"):
        return True
    if evidence.kind == "app_api":
        if "scenario" in data:
            return True
        status = data.get("status")
        return bool(data.get("error")) or (
            isinstance(status, int) and not isinstance(status, bool) and status >= 400
        )
    if evidence.kind == "detector":
        rule_id = data.get("rule_id")
        spec = CATALOG.get(rule_id) if isinstance(rule_id, str) else None
        return spec is not None and spec.grade == "signal" and data.get("verdict") == "detected"
    return False


def next_targets(engine: Engine, now: datetime, limit: int = 20) -> list[str]:
    """요약이 없는 대상 근거 번호를 최신순으로."""
    summaries, evidence = EvidenceSummaryStore(engine), EvidenceStore(engine)
    found: list[str] = []
    before = None
    while len(found) < limit:
        page = summaries.pending_evidence_ids(now - LOOKBACK, PAGE, before)
        if not page:
            break
        for ev_id in page:
            item = evidence.get(ev_id)
            if item is not None and is_presummary_target(item):
                found.append(ev_id)
                if len(found) == limit:
                    break
        before = _parse_id(page[-1])
    return found
