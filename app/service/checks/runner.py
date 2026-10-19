"""감지기 러너: 규칙 목록의 모든 감지기를 한 번씩 돌린다 (05-rules §0).

- 규칙 목록(catalog)의 모든 ID에 감지기가 있어야 한다. 없으면 시작하지 않는다.
- 감지기가 예외를 내거나 잘못된 결과를 내면 그 규칙을 "확인하지 못함"으로 둔다 (메시지는 마스킹).
- 판정 뒤 이번에 판정한 대상 밖의 관측 기록을 지운다(prune). 규칙 전체를 확인하지 못했으면
  기록을 그대로 둔다 (판정 보류, 05-rules §0.2).
"""

import importlib

from core.guard.redact import redact_text
from core.log.logging import get_logging
from service.checks.detectors.base import DetectorContext, registered
from service.checks.detectors.catalog import CATALOG
from service.model import Signal

logger = get_logging()


def load_detectors() -> None:
    """감지기 모듈(`detectors/<규칙 ID 소문자>.py`)을 등록하고 빠진 규칙이 없는지 확인한다."""
    for rule_id in CATALOG:
        importlib.import_module(f"service.checks.detectors.{rule_id.lower()}")
    missing = sorted(set(CATALOG) - set(registered()))
    if missing:
        raise RuntimeError(f"감지기가 없는 규칙: {', '.join(missing)}")


def run_detectors(ctx: DetectorContext) -> list[Signal]:
    load_detectors()
    detectors = registered()
    signals: list[Signal] = []
    for rule_id in CATALOG:
        result = _run_one(rule_id, detectors[rule_id], ctx)
        if not _rule_unknown(result, ctx):
            ctx.observations.prune(rule_id, {s.subject for s in result})
        signals.extend(result)
    return signals


def _run_one(rule_id: str, detect, ctx: DetectorContext) -> list[Signal]:
    try:
        result = detect(ctx)
        for s in result:
            if s.rule_id != rule_id:
                raise ValueError(f"다른 규칙의 결과: {s.rule_id}")
            if s.verdict == "detected" and s.severity is None:
                raise ValueError(f"적발인데 1차 심각도가 없음: {s.subject}")
        return result
    except Exception as e:  # 감지기 결함이 점검 전체를 멈추지 않게 한다
        message = redact_text(f"감지기 오류 {type(e).__name__}: {e}")
        logger.warning(f"{rule_id} {message}")
        return [
            Signal(
                rule_id=rule_id,
                verdict="unknown",
                subject=ctx.snapshot.namespace,
                message=message,
            )
        ]


def _rule_unknown(result: list[Signal], ctx: DetectorContext) -> bool:
    """규칙 전체를 확인하지 못함 = 네임스페이스를 대상으로 한 unknown (`rule_unknown`, 예외)."""
    return any(s.verdict == "unknown" and s.subject == ctx.snapshot.namespace for s in result)
