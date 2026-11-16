"""조사 흐름 로그: 단계가 끝날 때마다 한 줄 (시연·운영에서 `kubectl logs`로 흐름을 본다, #220).

- 단계 기록(StepSink)을 감싸 DB 기록은 그대로 두고 로그만 더한다. 조사 동작은 바꾸지 않는다.
- 문자열은 마스킹한 뒤 자른다 (자르다가 비밀값 일부가 남지 않게).
- 로그는 부가 작업이다. 실패해도 조사·기록은 이어진다.
"""

from functools import wraps
from typing import Any

from core.guard.redact import redact_text
from core.log.logging import get_logging
from service.agent.base import AgentResult, AgentStep, MemorySteps, StepSink
from service.model.report import JUDGEMENT_LABEL

logger = get_logging()

_VERDICT = {"supported": "지지", "refuted": "반증", "inconclusive": "판단 불가"}
_TITLE_MAX = 40
_CONCLUSION_MAX = 80
_FACT_ROWS = 6  # 사실 하나가 여러 줄일 때 싣는 최대 행 수
_AFTER_RECHECK = "재조사 후 "
_DETAIL_MAX = 120  # 세부 줄 한 줄의 상한
_AREA_LABEL = {"run": "구동", "config": "설정", "wiring": "연결", "logs": "로그"}


def _clip(text: Any, limit: int) -> str:
    text = " ".join(redact_text(str(text)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _titles(items: list[Any]) -> str:
    return " — " + " / ".join(_clip(t, _TITLE_MAX) for t in items) if items else ""


def _target(args: dict[str, Any]) -> str:
    if args.get("kind") and args.get("name"):
        return f"{args['kind']}/{args['name']}"
    values = [v for v in args.values() if v not in (None, "") and not isinstance(v, bool)]
    return " ".join(str(v) for v in values)


def _items(items: Any, render) -> list[str]:
    """세부 줄: `  - …` 들여쓴 줄. 목록이 아니면 없는 것으로 본다."""
    return (
        [f"  - {render(n, x)}" for n, x in enumerate(items, 1)] if isinstance(items, list) else []
    )


def _fact_lines(fact: dict[str, Any]) -> list[str]:
    """사실 하나의 세부 줄. 한 줄이면 근거 번호를 붙이고, 여러 줄(구간 근거표)이면 머리말을 빼고
    행마다 한 줄씩 `_FACT_ROWS`행까지 싣는다."""
    rows = [r for r in str(fact["statement"]).splitlines() if r.strip()]
    if len(rows) <= 1:
        return [f"  - {_clip(fact['statement'], _DETAIL_MAX)} [{', '.join(fact['evidence_ids'])}]"]
    rows = rows[1:] if rows[0].rstrip().endswith(":") else rows
    lines = [f"  - {_clip(r, _DETAIL_MAX)}" for r in rows[:_FACT_ROWS]]
    return lines + ([f"  - … 외 {len(rows) - _FACT_ROWS}행"] if len(rows) > _FACT_ROWS else [])


def _demoted_label(item: str) -> str:
    """validate가 남기는 `coverage.<영역>`·`cause_chain.<번호>` 표기를 사람이 읽는 말로."""
    kind, _, key = item.partition(".")
    if kind == "coverage" and key in _AREA_LABEL:
        return f"확인 범위({_AREA_LABEL[key]})"
    if kind == "cause_chain" and key.isdigit():
        return f"호출 체인 {int(key) + 1}번째 지점"
    return item


def _runbook(d: dict[str, Any]) -> str:
    name = d.get("runbook")
    if not name:
        return ""
    title = f"({_clip(d['runbook_title'], _TITLE_MAX)})" if d.get("runbook_title") else ""
    return f" · 조사 지침: {name}{title}"


def describe_step(step: AgentStep) -> list[str]:
    """남길 만한 단계만 줄로. 첫 줄이 요약이고 뒤는 `  - ` 세부 줄이다. 나머지(act 등)는 빈 목록."""
    d = step.detail
    match step.node:
        case "knowledge":
            return [
                f"지식 주입: 환경 설명 {d.get('environment', 0)}건 · "
                f"조사 지침 {len(d.get('runbooks', []))}종 · 약 {d.get('chars', 0):,}자 (전체 주입)"
            ]
        case "preliminary":
            facts = [x for f in d.get("facts") or [] for x in _fact_lines(f)]
            return [f"사전 확인: 근거 {len(step.evidence_ids)}건", *facts]
        case "triage":
            issues = d.get("issues", [])
            return [_clip(f"선별: 문제 {len(issues)}개{_titles(issues)}{_runbook(d)}", _DETAIL_MAX)]
        case "plan":
            hyps = d.get("hypotheses", [])
            guide = f" · 지침 {d['runbook']}" if d.get("runbook") else ""
            areas = d.get("areas") or []

            def hypothesis(n: int, h: str) -> str:
                area = _AREA_LABEL.get(areas[n - 1]) if n <= len(areas) else None
                return f"가설 {n}{f' ({area})' if area else ''}: {_clip(h, _DETAIL_MAX)}"

            return [f"계획: 가설 {len(hyps)}개{guide}", *_items(hyps, hypothesis)]
        case "tools":
            line = f"도구 {step.tool} {_clip(_target(step.args), 60)}".rstrip()
            line += f" → 근거 {', '.join(step.evidence_ids)}" if step.evidence_ids else ""
            return [("(재조사) " if d.get("recheck") else "") + line]
        case "write_report":
            attempt = d.get("attempt", 1)
            why = " — 결론에 근거 번호가 없어 다시 씀" if attempt > 1 else ""
            return [f"{_AFTER_RECHECK if d.get('recheck') else ''}보고서 작성(시도 {attempt}){why}"]
        case "validate" if "cited" in d:
            hidden = d.get("demoted_facts", [])
            demoted = [x for x in d.get("demoted", []) if x not in hidden]
            supported = "있음" if d.get("conclusion_supported") else "없음"
            after = _AFTER_RECHECK if d.get("recheck") else ""
            return [
                f"{after}근거 번호 대조: 인용 {d['cited']}건 중 {d['confirmed']}건 일치 · "
                f"없는 번호 제거 {d['removed']}건 · 강등 {len(demoted)}건 · 결론 근거 {supported}",
                *_items(demoted, lambda _, t: f"강등: {_clip(_demoted_label(t), _DETAIL_MAX)}"),
            ]
        case "stop_check":
            gaps = d.get("gaps", [])
            listed = _items(gaps, lambda _, g: _clip(g, _DETAIL_MAX))
            if d.get("recheck"):
                return [f"재조사: 빈틈 {len(gaps)}개 — 한 번 더 조사합니다", *listed]
            if not gaps:
                return ["빈틈 검사: 없음"]
            why = "재조사 상한" if d.get("rechecked") else "시간·호출 상한"
            idle = " · 재조사 중 도구 호출 없음" if d.get("recheck_no_tool_calls") else ""
            return [f"빈틈 검사: {len(gaps)}개 남음({why}){idle}", *listed]
        case "limit" | "limit_time" | "skip_calls":
            return [f"상한 도달 ({d.get('stop_reason')})"]
    return []


class FlowLogSteps(MemorySteps):
    """StepSink: 안쪽 sink(DB)에 먼저 쓰고, 한 줄 로그를 남긴다."""

    def __init__(self, label: str, inner: StepSink | None = None):
        super().__init__()
        self._label = label
        self._inner = inner

    def record(self, step: AgentStep) -> None:
        super().record(step)
        if self._inner is not None:
            self._inner.record(step)
        try:
            for line in describe_step(step):
                logger.info(f"[조사 {self._label}] {line}")
        except Exception as e:  # noqa: BLE001 — 로그가 조사를 멈추지 않게
            logger.debug(f"조사 단계 로그 실패: {type(e).__name__}")


def _never_raises(fn):
    """로그는 부가 작업이다. 실패해도 조사·기록을 막지 않는다."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"조사 흐름 로그 실패: {type(e).__name__}")

    return wrapper


@_never_raises
def log_start(label: str, request: Any, *, incident_id: int | None, reason: str | None) -> None:
    where = f"이슈 #{incident_id} " if incident_id is not None else ""
    why = f" — {_clip(reason, 80)}" if reason else ""
    logger.info(f"[조사 {label}] 시작: {where}경로 {request.gate_path}{why}")


@_never_raises
def log_end(label: str, result: AgentResult) -> None:
    seconds = f"{int(result.usage.seconds)}s"
    report = result.report
    if report is not None and report.hypotheses:
        verdicts = " / ".join(
            f"{_clip(h.statement, _TITLE_MAX)} {_VERDICT.get(h.status, h.status)}"
            for h in report.hypotheses
        )
        logger.info(f"[조사 {label}] 가설 판정: {verdicts}")
    if report is None:
        logger.info(f"[조사 {label}] 실패: {_clip(result.error or result.status, 80)} ({seconds})")
        return
    judged = f"판단 수준 {JUDGEMENT_LABEL[report.judgement]}, " if report.judgement else ""
    done = "" if result.status == "done" else f"{result.status}, "
    logger.info(
        f"[조사 {label}] 결론: {_clip(report.conclusion, _CONCLUSION_MAX)} "
        f"({done}{judged}{seconds}, 도구 {result.usage.tool_calls}회)"
    )
    if report.judgement_basis:
        logger.info(f"[조사 {label}] 판단 근거: {_clip(report.judgement_basis, _DETAIL_MAX)}")
