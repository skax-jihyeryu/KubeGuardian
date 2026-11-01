"""점검 DB에서 에이전트가 인용한 근거 원문을 읽는다 (근거 judge·핵심 근거 채점 입력)."""

from collections.abc import Iterable
from pathlib import Path

from core.db import EvidenceStore, create_db_engine


def agent_evidence_ids(steps: list[dict]) -> list[str]:
    """도구 호출(`tools`) 단계가 모은 근거 id를 순서대로, 중복 없이."""
    seen: dict[str, None] = {}
    for step in steps:
        if step.get("node") == "tools":
            for ev_id in step.get("evidence_ids") or []:
                seen.setdefault(ev_id)
    return list(seen)


def load_evidence(db_path: Path, ids: Iterable[str]) -> dict[str, dict]:
    """id → {id, kind, subject, data}. 없는 id는 빠진다. data는 파싱된 dict."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    store = EvidenceStore(create_db_engine(str(db_path)))
    result = {}
    for ev_id in ids:
        ev = store.get(ev_id)
        if ev is not None:
            result[ev_id] = {"id": ev.id, "kind": ev.kind, "subject": ev.subject, "data": ev.data}
    return result
