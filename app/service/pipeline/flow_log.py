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


def _clip(text: Any, limit: int) -> str:
    text = " ".join(redact_text(str(text)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _titles(items: list[Any]) -> str:
    return " — " + " / ".join(_clip(t, _TITLE_MAX) for t in items) if items else ""


def _target(args: dict[str, Any]) -> str:
    if args.get("kind") and args.get("name"):
        return f"{args['kind']}/{args['name']}"
    return " ".join(str(v) for v in args.values())


def describe_step(step: AgentStep) -> str | None:
    """남길 만한 단계만 한 줄로. 나머지(act·validate 등)는 None."""
    d = step.detail
    match step.node:
        case "preliminary":
            return f"사전 확인: 근거 {len(step.evidence_ids)}건"
        case "triage":
            issues = d.get("issues", [])
            return f"선별: 문제 {len(issues)}개{_titles(issues)}"
        case "plan":
            hyps = d.get("hypotheses", [])
            return f"계획: 가설 {len(hyps)}개{_titles(hyps)}"
        case "tools":
            line = f"도구 {step.tool} {_clip(_target(step.args), 60)}".rstrip()
            return line + (f" → 근거 {', '.join(step.evidence_ids)}" if step.evidence_ids else "")
        case "stop_check" if d.get("recheck"):
            return f"재조사: 빈틈 {len(d.get('gaps', []))}개"
        case "limit" | "limit_time" | "skip_calls":
            return f"상한 도달 ({d.get('stop_reason')})"
    return None


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
            if line := describe_step(step):
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
