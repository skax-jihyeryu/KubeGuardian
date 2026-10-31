"""history_qa 읽기 전용 도구 6개. 저장된 이력만 읽고 DB·클러스터에 아무것도 쓰지 않는다.

`service/tools/base.Tool`은 호출마다 evidence를 저장하므로 쓰지 않는다. 호출 상한은 `run`이 센다.
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Engine

from core.db import AlertStore, CheckRunStore, EvidenceStore, IncidentStore
from core.guard.budget import BudgetTracker
from core.guard.redact import redact_mapping
from service.notify.messages import DISPLAY_TZ

KNOWLEDGE_DIR = Path(__file__).parents[3] / "knowledge"
MAX_CHARS = 6000  # 도구 한 번의 결과 상한 (JSON 길이)
ALERT_TEXT_CHARS = 500
_NAME = r"^[a-z0-9-]+$"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Window(_Input):
    since: AwareDatetime | None = None
    limit: int = Field(default=20, ge=1, le=50)


def _at(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(DISPLAY_TZ).isoformat()


def _lists(node: Any):
    """트리 안의 모든 list."""
    if isinstance(node, list):
        yield node
        for v in node:
            yield from _lists(v)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _lists(v)


def _cut_strings(node: Any, limit: int) -> Any:
    if isinstance(node, str):
        return node[:limit]
    if isinstance(node, list):
        return [_cut_strings(v, limit) for v in node]
    if isinstance(node, dict):
        return {k: _cut_strings(v, limit) for k, v in node.items()}
    return node


def _size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=str))


def _fit(data: dict) -> dict:
    """MAX_CHARS 안이 되도록 가장 큰 목록을 절반씩 줄이고, 목록이 바닥나면 문자열을 자른다."""
    if _size(data) <= MAX_CHARS:
        return data
    data = {**data, "truncated": True}
    while _size(data) > MAX_CHARS:
        lists = [lst for lst in _lists(data) if lst]
        if lists:
            biggest = max(lists, key=lambda lst: len(json.dumps(lst, default=str)))
            del biggest[len(biggest) // 2 :]
            continue
        for limit in (200, 50, 10):
            data = _cut_strings(data, limit)
            if _size(data) <= MAX_CHARS:
                break
        else:
            break
    return data


class HistoryTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Input: ClassVar[type[BaseModel]]

    def __init__(self, engine: Engine, knowledge_dir: Path):
        self._engine = engine
        self._knowledge_dir = knowledge_dir

    def run(self, args: dict, budget: BudgetTracker) -> dict:
        try:
            inp = self.Input.model_validate(args)
        except ValidationError as e:
            fields = [".".join(str(p) for p in err["loc"]) for err in e.errors()]
            return {"ok": False, "error": "VALIDATION_ERROR", "fields": fields}
        if not budget.can_call_tool():
            return {"ok": False, "error": "BUDGET_EXCEEDED"}
        budget.count_tool_call()
        result = redact_mapping(self._query(inp))
        return _fit(result) if result.get("ok") else result

    @abstractmethod
    def _query(self, inp: Any) -> dict: ...


class ListRuns(HistoryTool):
    name = "list_runs"
    description = (
        "점검 목록(최신순). 시작 시각·트리거·스냅샷 유무·그 점검에서 열리거나 해결된 이슈."
    )

    class Input(_Window):
        until: AwareDatetime | None = None

    def _query(self, inp: Any) -> dict:
        runs = CheckRunStore(self._engine).list_runs(inp.since, inp.until, inp.limit)
        actions = IncidentStore(self._engine).actions_for_runs([r[0] for r in runs])
        return {
            "ok": True,
            "runs": [
                {
                    "id": run_id,
                    "started_at": _at(started_at),
                    "trigger": trigger,
                    "collected": collected,
                    "incidents": [{"id": i, "action": a} for i, a in actions.get(run_id, [])],
                }
                for run_id, started_at, trigger, collected in runs
            ],
        }


class GetSnapshot(HistoryTool):
    name = "get_snapshot"
    description = (
        "점검의 스냅샷. kind를 주지 않으면 kind별 개수, 주면 그 kind의 객체(name은 부분 일치)."
    )

    class Input(_Input):
        run_id: int
        kind: str | None = None
        name: str | None = None

    def _query(self, inp: Any) -> dict:
        snap = CheckRunStore(self._engine).get_snapshot(inp.run_id)
        if snap is None:
            return {"ok": False, "error": "NOT_FOUND"}
        head = {
            "ok": True,
            "run_id": inp.run_id,
            "collected_at": _at(snap.collected_at),
            "namespace": snap.namespace,
        }
        if inp.kind is None:
            kinds = {k: len(v) for k, v in snap.resources.items()}
            return {**head, "kinds": kinds, "failed": snap.failed}
        by_lower = {k.lower(): k for k in snap.resources}
        kind = by_lower.get(inp.kind.lower())
        if kind is None:
            return {"ok": False, "error": "NOT_FOUND", "kinds": list(snap.resources)}
        items = snap.resources[kind]
        if inp.name:
            needle = inp.name.lower()
            items = [i for i in items if needle in _item_name(i).lower()]
        return {**head, "kind": kind, "items": items}


def _item_name(item: dict) -> str:
    name = (item.get("metadata") or {}).get("name") or item.get("name") or ""
    return str(name)


class GetEvidence(HistoryTool):
    name = "get_evidence"
    description = (
        "근거. evidence_id를 주면 그 한 건(ev-0001 형식), "
        "run_id를 주면 그 점검의 근거 목록(id·종류·대상)."
    )

    class Input(_Input):
        evidence_id: str | None = Field(default=None, pattern=r"^ev-\d+$")
        run_id: int | None = None

        @model_validator(mode="after")
        def _exactly_one(self) -> "GetEvidence.Input":
            if (self.evidence_id is None) == (self.run_id is None):
                raise ValueError("evidence_id와 run_id 중 하나만 준다")
            return self

    def _query(self, inp: Any) -> dict:
        if inp.run_id is not None:
            found = EvidenceStore(self._engine).list_for_run(inp.run_id)
            return {
                "ok": True,
                "run_id": inp.run_id,
                "evidences": [
                    {"id": e.id, "kind": e.kind, "subject": e.subject, "source": e.source}
                    for e in found
                ],
            }
        ev = EvidenceStore(self._engine).get(inp.evidence_id)
        if ev is None:
            return {"ok": False, "error": "NOT_FOUND"}
        return {
            "ok": True,
            "id": ev.id,
            "kind": ev.kind,
            "subject": ev.subject,
            "collected_at": _at(ev.collected_at),
            "source": ev.source,
            "data": ev.data,
        }


class ListIssues(HistoryTool):
    name = "list_issues"
    description = "이슈 목록(최신순). status·keyword(서비스·기능·규칙·대상 부분 일치)로 거른다."

    class Input(_Window):
        status: Literal["open", "resolved"] | None = None
        keyword: str | None = None

    def _query(self, inp: Any) -> dict:
        store = IncidentStore(self._engine)
        if inp.keyword is None:
            found = store.list_incidents(inp.since, inp.status, inp.limit)
        else:
            needle = inp.keyword.lower()
            # 저장소는 keyword를 모르므로 넉넉히 읽어 거른 뒤 자른다
            found = [
                i
                for i in store.list_incidents(inp.since, inp.status, 500)
                if needle in _haystack(i).lower()
            ][: inp.limit]
        return {
            "ok": True,
            "issues": [
                {
                    "id": i.id,
                    "status": i.status,
                    "first_severity": i.first_severity,
                    "final_severity": i.final_severity,
                    "opened_at": _at(i.opened_at),
                    "resolved_at": _at(i.resolved_at),
                    "minutes": int(
                        ((i.resolved_at or i.last_seen_at) - i.opened_at).total_seconds() // 60
                    ),
                    "services": list(i.services),
                    "features": list(i.features),
                    "rule_ids": list(i.rule_ids),
                    "investigation": i.investigation,
                }
                for i in found
            ],
        }


def _haystack(i: Any) -> str:
    targets = [" ".join(t) for t in i.targets]
    return " ".join([*i.services, *i.features, *i.rule_ids, *targets])


class ListAlerts(HistoryTool):
    name = "list_alerts"
    description = "보낸(또는 dry-run으로 기록한) 알림 목록(최신순). 본문은 500자까지."

    class Input(_Window):
        issue_id: int | None = None

    def _query(self, inp: Any) -> dict:
        alerts = AlertStore(self._engine).list_alerts(inp.since, inp.issue_id, inp.limit)
        return {
            "ok": True,
            "alerts": [
                {
                    "id": a.id,
                    "issue_id": a.incident_id,
                    "kind": a.kind,
                    "sent_at": _at(a.sent_at),
                    "dry_run": a.dry_run,
                    "text": a.text[:ALERT_TEXT_CHARS],
                    "error": a.error,
                }
                for a in alerts
            ],
        }


class ReadKnowledge(HistoryTool):
    name = "read_knowledge"
    description = "환경 문서와 런북. name을 주지 않으면 문서 이름 목록."

    class Input(_Input):
        name: str | None = Field(default=None, pattern=_NAME)

    def _query(self, inp: Any) -> dict:
        runbooks = self._knowledge_dir / "runbooks"
        if inp.name is None:
            names = sorted(p.stem for p in runbooks.glob("*.md") if not p.stem.startswith("_"))
            return {"ok": True, "docs": ["environment", *names]}
        path = (
            self._knowledge_dir / "environment.md"
            if inp.name == "environment"
            else runbooks / f"{inp.name}.md"
        )
        if not path.is_file():
            return {"ok": False, "error": "NOT_FOUND"}
        return {"ok": True, "name": inp.name, "text": path.read_text(encoding="utf-8")}


def build_history_tools(engine: Engine, knowledge_dir: Path) -> list[HistoryTool]:
    classes = (ListRuns, GetSnapshot, GetEvidence, ListIssues, ListAlerts, ReadKnowledge)
    return [cls(engine, knowledge_dir) for cls in classes]
