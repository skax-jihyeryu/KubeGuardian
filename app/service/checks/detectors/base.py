"""감지기 인터페이스 (05-rules §0).

감지기는 ClusterSnapshot과 관측 기록만 보고 판정하는 결정적 함수다.
`@detector("S04")`로 등록하며, 규칙 목록에 없는 ID는 등록할 수 없다.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from service.checks.detectors.catalog import CATALOG, RuleSpec
from service.model import ClusterSnapshot, Signal, Verdict


class ObservationStore(Protocol):
    """지속 시간 판정용 관측 기록 (05-rules §0.2). 구현은 core/db."""

    def touch(self, rule_id: str, subject: str, now: datetime) -> datetime:
        """조건을 만족함을 기록하고 최초 관측 시각을 돌려준다."""
        ...

    def clear(self, rule_id: str, subject: str) -> None:
        """조건이 풀렸을 때 기록을 지운다 (연속성이 끊김)."""
        ...


@dataclass(frozen=True)
class DetectorContext:
    snapshot: ClusterSnapshot
    observations: ObservationStore
    now: datetime


Detector = Callable[[DetectorContext], list[Signal]]
_REGISTRY: dict[str, Detector] = {}


def detector(rule_id: str) -> Callable[[Detector], Detector]:
    if rule_id not in CATALOG:
        raise ValueError(f"규칙 목록에 없는 ID: {rule_id}")

    def register(fn: Detector) -> Detector:
        if rule_id in _REGISTRY:
            raise ValueError(f"이미 등록된 감지기: {rule_id}")
        _REGISTRY[rule_id] = fn
        return fn

    return register


def registered() -> dict[str, Detector]:
    return _REGISTRY


def evaluate_hold(rule: RuleSpec, first_seen: datetime, now: datetime) -> Verdict:
    """조건을 만족한 상태에서 지속 시간을 채웠으면 적발, 아니면 관찰 중."""
    if rule.hold is None or now - first_seen >= rule.hold:
        return "detected"
    return "observing"
