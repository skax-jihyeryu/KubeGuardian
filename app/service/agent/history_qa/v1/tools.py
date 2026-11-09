"""history_qa 읽기 전용 이력·보고서 도구. 저장된 이력만 읽고 DB·클러스터에 아무것도 쓰지 않는다.

`service/tools/base.Tool`은 호출마다 evidence를 저장하므로 쓰지 않는다. 호출 상한은 `run`이 센다.
"""

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Engine, String, cast, exists, func, select
from sqlalchemy.orm import Session

from core.db import AlertStore, CheckRunStore, EvidenceStore, IncidentStore
from core.db.tables import InvestigationRow
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
    until: AwareDatetime | None = None
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

    Input = _Window

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
        return {**head, "kind": kind, "total": len(items), "items": items}


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
    description = (
        "이슈 목록(최신순). status·keyword(서비스·기능·규칙·대상 부분 일치)로 거르고, "
        "since·until로 열린 시각 기간을 좁힌다."
    )

    class Input(_Window):
        status: Literal["open", "resolved"] | None = None
        keyword: str | None = None

    def _query(self, inp: Any) -> dict:
        store = IncidentStore(self._engine)
        if inp.keyword is None:
            found = store.list_incidents(inp.since, inp.status, inp.limit, inp.until)
        else:
            needle = inp.keyword.lower()
            # 저장소는 keyword를 모르므로 넉넉히 읽어 거른 뒤 자른다
            found = [
                i
                for i in store.list_incidents(inp.since, inp.status, 500, inp.until)
                if needle in _haystack(i).lower()
            ][: inp.limit]
        return {
            "ok": True,
            "as_of_run": _latest_run_id(self._engine),
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


def _latest_run_id(engine: Engine) -> int | None:
    """조회 기준 시점. 없음 답이 인용할 수 있는 최신 점검 id."""
    latest = CheckRunStore(engine).latest_run()
    return None if latest is None else latest[0]


def _haystack(i: Any) -> str:
    targets = [" ".join(t) for t in i.targets]
    return " ".join([*i.services, *i.features, *i.rule_ids, *targets])


class ListAlerts(HistoryTool):
    name = "list_alerts"
    description = (
        "보낸(또는 dry-run으로 기록한) 알림 목록(최신순). 본문은 500자까지. "
        "since·until로 보낸 시각 기간을 좁힌다."
    )

    class Input(_Window):
        issue_id: int | None = None

    def _query(self, inp: Any) -> dict:
        alerts = AlertStore(self._engine).list_alerts(inp.since, inp.issue_id, inp.limit, inp.until)
        return {
            "ok": True,
            "as_of_run": _latest_run_id(self._engine),
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


# SQLite 정수 범위 안의 양수만 받는다. bool·문자열을 번호로 조용히 바꾸지 않는다.
ReportId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


def _report_data(engine: Engine, investigation) -> dict:
    """다른 점검의 근거와 존재하지 않는 근거는 보고서 인용으로 전달하지 않는다."""
    evidence = EvidenceStore(engine)
    data = investigation.report.model_dump(mode="json")
    allowed: dict[str, bool] = {}

    def belongs(ev):
        if not isinstance(ev, str) or not re.fullmatch(r"ev-[0-9]{1,19}", ev):
            return False
        if not 0 < int(ev[3:]) <= 2**63 - 1:
            return False
        if ev not in allowed:
            allowed[ev] = evidence.check_run_of(ev) == investigation.check_run_id
        return allowed[ev]

    def clean(node):
        if isinstance(node, dict):
            return {
                key: [ev for ev in value if belongs(ev)]
                if key == "evidence_ids" and isinstance(value, list)
                else clean(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [clean(value) for value in node]
        return node

    return redact_mapping(clean(data))


def _report_evidence(data: dict) -> list[str]:
    ids = set()

    def collect(node):
        if isinstance(node, dict):
            ids.update(node.get("evidence_ids", []))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(data)
    return sorted(ids)


class SearchReports(HistoryTool):
    name = "search_reports"
    description = (
        "저장된 AI 조사 보고서 검색(최신순). keyword는 보고서 내용 부분 일치, "
        "issue_id·run_id·since·until로 범위를 좁힌다. id는 get_report에 줄 보고서 번호다."
    )

    class Input(_Window):
        issue_id: ReportId | None = None
        run_id: ReportId | None = None
        keyword: str | None = Field(default=None, max_length=200)

    def _query(self, inp: Any) -> dict:
        stmt = select(InvestigationRow.id).where(
            func.json_type(InvestigationRow.report) == "object"
        )
        if inp.since is not None:
            stmt = stmt.where(InvestigationRow.started_at >= inp.since)
        if inp.until is not None:
            stmt = stmt.where(InvestigationRow.started_at <= inp.until)
        if inp.issue_id is not None:
            stmt = stmt.where(InvestigationRow.incident_id == inp.issue_id)
        if inp.run_id is not None:
            stmt = stmt.where(InvestigationRow.check_run_id == inp.run_id)
        if inp.keyword:
            # JSON 저장 시 한글이 \u 이스케이프로 들어가도 값 자체에서 검색한다.
            text = func.json_tree(InvestigationRow.report).table_valued("type", "value")
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(text)
                    .where(
                        text.c.type == "text",
                        cast(text.c.value, String).contains(inp.keyword, autoescape=True),
                    )
                    .correlate(InvestigationRow)
                )
            )
        with Session(self._engine) as session:
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            ids = list(session.scalars(stmt.order_by(InvestigationRow.id.desc()).limit(inp.limit)))
        from core.db.investigations import InvestigationStore

        store = InvestigationStore(self._engine)
        reports = []
        for report_id in ids:
            investigation = store.get(report_id)
            if investigation is None:
                continue
            data = _report_data(self._engine, investigation) if investigation.report else {}
            reports.append(
                {
                    "id": report_id,
                    "run_id": investigation.check_run_id,
                    "issue_id": investigation.incident_id,
                    "status": investigation.status,
                    "started_at": _at(investigation.started_at),
                    "report_invalid": investigation.report_invalid,
                    "conclusion": str(data.get("conclusion", ""))[:600],
                    "judgement": data.get("judgement"),
                    "evidence_ids": _report_evidence(data),
                }
            )
        return {
            "ok": True,
            "as_of_run": _latest_run_id(self._engine),
            "total": total,
            "reports": reports,
            "truncated": total > len(reports),
        }


class GetReport(HistoryTool):
    name = "get_report"
    description = (
        "보고서 번호(report_id, search_reports의 id)의 저장된 보고서. "
        "결론·문제·권장 조치·가설 판정·확인 범위·재검증·근거 번호를 읽는다. "
        "선택 issue_id·run_id를 주면 그 이슈·점검 소속일 때만 반환한다."
    )

    class Input(_Input):
        report_id: ReportId
        issue_id: ReportId | None = None
        run_id: ReportId | None = None

    def _query(self, inp: Any) -> dict:
        from core.db.investigations import InvestigationStore

        investigation = InvestigationStore(self._engine).get(inp.report_id)
        if (
            investigation is None
            or inp.issue_id is not None
            and investigation.incident_id != inp.issue_id
            or inp.run_id is not None
            and investigation.check_run_id != inp.run_id
        ):
            return {"ok": False, "error": "NOT_FOUND"}
        if investigation.report is None:
            return {"ok": False, "error": "REPORT_UNAVAILABLE"}
        data = _report_data(self._engine, investigation)
        return {
            "ok": True,
            "id": investigation.id,
            "run_id": investigation.check_run_id,
            "issue_id": investigation.incident_id,
            "status": investigation.status,
            "started_at": _at(investigation.started_at),
            "evidence_ids": _report_evidence(data),
            "report": data,
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
    classes = (
        ListRuns,
        GetSnapshot,
        GetEvidence,
        ListIssues,
        ListAlerts,
        SearchReports,
        GetReport,
        ReadKnowledge,
    )
    return [cls(engine, knowledge_dir) for cls in classes]
