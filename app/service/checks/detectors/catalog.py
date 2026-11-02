"""규칙 목록. docs/05-rules.md(명세의 정본)를 코드로 옮긴 것이다.

명세와 이 목록이 일치하는지는 tests/service/checks/test_catalog.py가 확인한다.
규칙을 바꿀 때는 명세를 먼저 고친다.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from service.model import Severity

Nature = Literal["state", "declared", "time"]  # 상태 / 선언 / 시간
Grade = Literal["signal", "advice"]  # 장애 신호(게이트를 연다) / 개선 권고


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    nature: Nature
    hold: timedelta | None  # 판정 시점: 지속 시간. None이면 즉시 판정
    severities: tuple[Severity, ...]  # 1차 심각도로 나올 수 있는 값 (05-rules §3)
    runbooks: tuple[str, ...]  # 관련 조사 지침

    @property
    def inputs(self) -> tuple[tuple[str, str], ...]:
        """판정과 대상 묶음에 사용하는 Kind·주요 필드 (외부 설정·이전 스냅샷 제외)."""
        return _INPUTS[self.id]

    @property
    def grade(self) -> Grade:
        return "advice" if self.id.startswith("H") else "signal"


M15 = timedelta(minutes=15)
_RULES = [
    RuleSpec("N01", "노드 NotReady", "state", M15, ("critical",), ("partial-failure",)),
    RuleSpec(
        "P03",
        "이미지 받기 실패",
        "state",
        timedelta(hours=1),
        ("warning", "critical"),
        ("failure-after-deploy",),
    ),
    RuleSpec(
        "P06",
        "반복 재시작 (CrashLoop)",
        "time",
        M15,
        ("warning", "critical"),
        ("failure-after-deploy", "noise-filtering"),
    ),
    RuleSpec(
        "P07", "OOMKilled", "state", None, ("warning",), ("partial-failure", "noise-filtering")
    ),
    RuleSpec(
        "W01",
        "가용 replicas 부족",
        "state",
        M15,
        ("warning", "critical"),
        ("failure-after-deploy",),
    ),
    RuleSpec(
        "W02", "롤아웃 정체", "state", M15, ("warning", "critical"), ("failure-after-deploy",)
    ),
    RuleSpec(
        "S01",
        "selector에 맞는 Pod 없음",
        "declared",
        None,
        ("critical", "warning"),
        ("service-unreachable",),
    ),
    RuleSpec(
        "S02",
        "Ready 엔드포인트 없음·일부",
        "state",
        M15,
        ("critical", "warning"),
        ("service-unreachable", "partial-failure"),
    ),
    RuleSpec(
        "S03",
        "targetPort 불일치",
        "declared",
        None,
        ("critical", "warning"),
        ("service-unreachable",),
    ),
    RuleSpec(
        "S04",
        "서비스 주소 설정 오류",
        "declared",
        None,
        ("critical", "warning"),
        ("service-unreachable",),
    ),
    RuleSpec(
        "C01",
        "설정 참조 누락",
        "declared",
        None,
        ("critical", "warning"),
        ("failure-after-deploy",),
    ),
    RuleSpec(
        "C05",
        "필수 설정 누락",
        "declared",
        None,
        ("critical", "warning"),
        ("failure-after-deploy", "service-unreachable"),
    ),
    RuleSpec("H01", "readinessProbe 없음·TCP만", "declared", None, ("info",), ("partial-failure",)),
    RuleSpec("H02", "requests·limits 미설정", "declared", None, ("info",), ()),
]

CATALOG: dict[str, RuleSpec] = {rule.id: rule for rule in _RULES}


_INPUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "N01": (("Node", "status.conditions (Ready)"),),
    "P03": (
        ("Pod", "status.containerStatuses, status.initContainerStatuses, metadata.ownerReferences"),
        ("ReplicaSet", "metadata.ownerReferences"),
        ("Service", "spec.selector"),
    ),
    "P06": (
        (
            "Pod",
            "status.containerStatuses (restartCount), status.initContainerStatuses, "
            "metadata.uid, metadata.ownerReferences",
        ),
        ("ReplicaSet", "metadata.ownerReferences"),
        ("Service", "spec.selector"),
    ),
    "P07": (
        (
            "Pod",
            "status.containerStatuses (state, lastState), status.initContainerStatuses, "
            "metadata.ownerReferences",
        ),
        ("ReplicaSet", "metadata.ownerReferences"),
        ("Service", "spec.selector"),
    ),
    "W01": (
        ("Deployment", "spec.replicas, status.availableReplicas, metadata.name"),
        ("Service", "spec.selector"),
    ),
    "W02": (
        ("Deployment", "spec, status.conditions, status.observedGeneration, metadata.generation"),
        ("ReplicaSet", "metadata.ownerReferences, metadata.annotations, spec.replicas, status"),
        ("Service", "spec.selector"),
    ),
    "S01": (("Service", "spec.selector, spec.type, spec.externalName"), ("Pod", "metadata.labels")),
    "S02": (
        ("Service", "metadata.name, spec.selector, spec.type"),
        ("EndpointSlice", "metadata.labels, endpoints.conditions, endpoints.targetRef"),
        ("Pod", "metadata.labels, metadata.ownerReferences, status.conditions"),
        ("ReplicaSet", "metadata.ownerReferences"),
    ),
    "S03": (
        ("Service", "spec.selector, spec.ports, spec.type"),
        ("Pod", "metadata.labels, spec.containers.ports"),
    ),
    "S04": (
        ("Service", "metadata.name, spec.selector, spec.ports"),
        (
            "Deployment",
            "spec.template.spec.containers.env, spec.template.spec.containers.envFrom, "
            "spec.template.metadata.labels",
        ),
        ("ConfigMap", "data"),
        ("Secret", "keys"),
    ),
    "C01": (
        ("Pod", "spec.containers, spec.initContainers, spec.volumes"),
        ("ConfigMap", "metadata.name, data, binaryDataKeys"),
        ("Secret", "metadata.name, keys"),
        ("ReplicaSet", "metadata.ownerReferences"),
        ("Service", "spec.selector"),
    ),
    "C05": (
        ("Deployment", "spec.template.spec.containers.env, spec.template.spec.containers.envFrom"),
        ("ConfigMap", "metadata.name, data"),
        ("Secret", "metadata.name, keys"),
        ("Service", "spec.selector"),
    ),
    "H01": (
        ("Pod", "spec.containers.readinessProbe"),
        ("Service", "spec.selector"),
        ("ReplicaSet", "metadata.ownerReferences"),
    ),
    "H02": (
        (
            "Pod",
            "spec.containers.resources, spec.initContainers.resources, metadata.ownerReferences",
        ),
        ("ReplicaSet", "metadata.ownerReferences"),
        ("Service", "spec.selector"),
    ),
}
