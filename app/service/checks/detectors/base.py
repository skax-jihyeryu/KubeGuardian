"""감지기 인터페이스 (05-rules §0).

감지기는 ClusterSnapshot과 관측 기록만 보고 판정하는 결정적 함수다.
`@detector("S04")`로 등록하며, 규칙 목록에 없는 ID는 등록할 수 없다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from core.config import KubeGuardianConfig
from service.checks.detectors.catalog import CATALOG, RuleSpec
from service.model import ClusterSnapshot, Signal, Verdict


@dataclass(frozen=True)
class Observation:
    """관측 기록 하나 (05-rules §0.2).

    values에는 판정에 쓴 값을 남긴다 (W01의 updatedReplicas 등).
    """

    first_seen: datetime
    last_seen: datetime
    values: dict[str, Any] = field(default_factory=dict)


class ObservationStore(Protocol):
    """지속 시간 판정용 관측 기록. 키는 (규칙 ID, 대상 리소스). 구현은 core/db."""

    def get(self, rule_id: str, subject: str) -> Observation | None: ...

    def touch(
        self, rule_id: str, subject: str, now: datetime, values: dict[str, Any] | None = None
    ) -> Observation:
        """조건을 만족함을 기록한다. 기록이 없으면 최초 관측 시각을 지금으로 둔다.

        values를 넘기면 저장된 값을 통째로 바꾸고, None이면 기존 값을 그대로 둔다.
        """
        ...

    def clear(self, rule_id: str, subject: str) -> None:
        """조건이 풀렸을 때 기록을 지운다 (연속성이 끊김)."""
        ...

    def prune(self, rule_id: str, keep_subjects: set[str]) -> None:
        """이번 점검에서 판정한 대상 밖의 기록을 지운다 (삭제된 리소스가 다시 생겨도 처음부터 잰다).

        러너는 그 규칙이 "확인하지 못함"이면 부르지 않는다 (판정 보류, 05-rules §0.2).
        """
        ...


@dataclass(frozen=True)
class DetectorContext:
    """감지기 입력. 감지기는 이것 밖의 값을 읽지 않는다 (결정적 판정).

    1차 심각도는 감지기가 규칙별 조건(05-rules §4)과 config.core_paths로 정해 Signal에 담고,
    게이트는 적발된 신호 중 가장 높은 심각도를 쓴다.
    """

    snapshot: ClusterSnapshot
    previous: (
        ClusterSnapshot | None
    )  # 직전 점검 스냅샷. 변화로 판정하는 규칙용 (P06 restartCount 증가)
    observations: ObservationStore
    config: KubeGuardianConfig  # core_paths, external_hosts, topology.url_env_patterns
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
