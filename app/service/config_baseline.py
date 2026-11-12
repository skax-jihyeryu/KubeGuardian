"""서비스 설정의 선언 비교와 운영자가 지정한 정상 관측 기준 (#179)."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel

from core.config import ApiScenario, KubeGuardianConfig, ServiceConfigBaseline
from service.checks.detectors._common import Workloads, name_of
from service.model import ClusterSnapshot, Signal, Symptom
from service.model.snapshot import ConfigBaselineObservation, ConfigBaselineProvenance
from service.sensors.topology import _edge, _service_ports

_KINDS = ("Deployment", "Service", "ConfigMap", "Secret", "Pod", "ReplicaSet")
_RELATION_KINDS = ("Deployment", "Service", "ConfigMap", "Secret")
Obj = dict[str, Any]
PinnedLoader = Callable[[int], tuple[ClusterSnapshot | None, bool]]


class ExpectedRelation(BaseModel):
    result: Literal["matched", "mismatch", "missing", "unsupported", "unknown"]
    observed_service: str | None = None
    limitations: tuple[str, ...] = ()


class PinnedBaseline(BaseModel):
    result: Literal["approved", "unavailable"]
    run_id: int | None = None
    collected_at: AwareDatetime | None = None
    limitations: tuple[str, ...] = ()


class ServiceBaselineReport(BaseModel):
    service: str
    deployment: str
    container: str
    env: str
    config_map: str
    key: str
    expected_service: str
    expected: ExpectedRelation
    baseline: PinnedBaseline


def _scenarios(config: KubeGuardianConfig, service: str) -> list[ApiScenario]:
    return sorted(
        (s for s in config.api_scenarios if service in config.core_paths[s.feature]),
        key=lambda s: s.name,
    )


def _intent(config: KubeGuardianConfig, service: str, spec: ServiceConfigBaseline) -> str:
    data = {
        "version": 1,
        "reference": spec.model_dump(exclude={"run_id", "max_age_hours"}),
        "required": sorted(config.required_config.get(service, ())),
        "scenarios": [s.model_dump(mode="json") for s in _scenarios(config, service)],
        "external_hosts": sorted(config.external_hosts),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def observe_baselines(
    config: KubeGuardianConfig,
    snapshot: ClusterSnapshot,
    signals: list[Signal],
    symptoms: list[Symptom],
) -> None:
    """대상별 규칙·시나리오 통과 여부만 기록한다. 명시적인 선택은 다음 점검에서 한다."""
    if not config.config_baselines or snapshot.namespace != config.target.namespace:
        return
    passed_scenarios = {s.scenario for s in symptoms if s.status == "passed"}
    pods = snapshot.items("Pod") or []
    workloads = Workloads(pods, snapshot.items("ReplicaSet"), snapshot.items("Service"))
    maps = _objects_by_name(snapshot, "ConfigMap")
    secrets = _objects_by_name(snapshot, "Secret")
    observations = {}
    for service, spec in config.config_baselines.items():
        scenarios = _scenarios(config, service)
        target_pods = [pod for pod in pods if workloads.deployment_of(pod) == spec.deployment]
        pod_subjects = {f"{snapshot.namespace}/Pod/{name_of(pod)}" for pod in target_pods}
        reference_checks = {
            s.subject: s for s in signals if s.rule_id == "C01" and s.subject in pod_subjects
        }
        observations[service] = ConfigBaselineObservation(
            intent_digest=_intent(config, service, spec),
            observations_passed=bool(scenarios)
            and all(s.steps and s.name in passed_scenarios for s in scenarios)
            and bool(pod_subjects)
            and reference_checks.keys() == pod_subjects
            and all(_passed(s) for s in reference_checks.values())
            and all(
                _binding(_container(pod, spec.container) or {}, spec, maps, secrets) == "matched"
                for pod in target_pods
            )
            and (service not in config.required_config or _passed_required(signals, service)),
        )
    snapshot.config_baseline = ConfigBaselineProvenance(
        project_id=config.project.id, observations=observations
    )


def _passed(signal: Signal) -> bool:
    return signal.verdict == "passed" and "failed_kinds" not in signal.values


def _passed_required(signals: list[Signal], service: str) -> bool:
    relevant = [s for s in signals if s.rule_id == "C05" and s.service in {None, service}]
    return any(s.service == service for s in relevant) and all(_passed(s) for s in relevant)


def report_baselines(
    config: KubeGuardianConfig,
    snapshot: ClusterSnapshot,
    *,
    now: datetime,
    run_id: int,
    load_pinned: PinnedLoader,
) -> list[ServiceBaselineReport]:
    """키 이름과 Kubernetes 객체 참조만 담은 진단 결과를 만든다."""
    loaded: dict[int, tuple[ClusterSnapshot | None, bool]] = {}
    reports = []
    for service, spec in sorted(config.config_baselines.items()):
        if spec.run_id is not None and spec.run_id < run_id and spec.run_id not in loaded:
            loaded[spec.run_id] = load_pinned(spec.run_id)
        historical, done = loaded.get(spec.run_id, (None, False))
        reports.append(
            ServiceBaselineReport(
                service=service,
                **spec.model_dump(exclude={"run_id", "max_age_hours"}),
                expected=_relation(config, snapshot, service, spec),
                baseline=_pinned(config, service, spec, historical, done, now, run_id),
            )
        )
    return reports


def _pinned(
    config: KubeGuardianConfig,
    service: str,
    spec: ServiceConfigBaseline,
    snapshot: ClusterSnapshot | None,
    done: bool,
    now: datetime,
    run_id: int,
) -> PinnedBaseline:
    provenance = snapshot.config_baseline if snapshot else None
    observation = provenance.observations.get(service) if provenance else None
    reason = None
    if spec.run_id is None:
        reason = "not_pinned"
    elif spec.run_id >= run_id:
        reason = "not_historical"
    elif snapshot is None:
        reason = "snapshot_missing"
    elif snapshot.namespace != config.target.namespace or (
        provenance is not None and provenance.project_id != config.project.id
    ):
        reason = "target_mismatch"
    elif provenance is None:
        reason = "legacy"
    elif observation is None or observation.intent_digest != _intent(config, service, spec):
        reason = "intent_mismatch"
    elif not done:
        reason = "check_incomplete"
    elif not 0 <= (now - snapshot.collected_at).total_seconds() <= 3600 * spec.max_age_hours:
        reason = "stale"
    elif any(snapshot.items(kind) is None for kind in _KINDS):
        reason = "collection_incomplete"
    elif not observation.observations_passed:
        reason = "observations_not_passed"
    elif _relation(config, snapshot, service, spec).result != "matched":
        reason = "expected_relation_not_met"
    return PinnedBaseline(
        result="unavailable" if reason else "approved",
        run_id=spec.run_id,
        collected_at=snapshot.collected_at if snapshot else None,
        limitations=(reason,) if reason else (),
    )


def _objects_by_name(snapshot: ClusterSnapshot, kind: str) -> dict[str, Obj]:
    return {name_of(obj): obj for obj in snapshot.items(kind) or []}


def _container(pod_or_template: Obj, name: str) -> Obj | None:
    return next(
        (c for c in (pod_or_template.get("spec") or {}).get("containers", []) if c["name"] == name),
        None,
    )


def _relation(
    config: KubeGuardianConfig, snapshot: ClusterSnapshot, service: str, spec: ServiceConfigBaseline
) -> ExpectedRelation:
    if snapshot.namespace != config.target.namespace:
        return ExpectedRelation(result="unknown", limitations=("target_mismatch",))
    missing_kinds = tuple(kind for kind in _RELATION_KINDS if snapshot.items(kind) is None)
    if missing_kinds:
        return ExpectedRelation(result="unknown", limitations=missing_kinds)
    deployments, services, maps, secrets = (
        _objects_by_name(snapshot, kind) for kind in _RELATION_KINDS
    )
    deployment = deployments.get(spec.deployment)
    if deployment is None:
        return ExpectedRelation(result="missing", limitations=("deployment_missing",))
    template = deployment["spec"].get("template") or {}
    workloads = Workloads([], None, list(services.values()))
    if workloads.service_of_deployment(deployment) != service:
        return ExpectedRelation(result="mismatch", limitations=("source_service_selector",))
    container = _container(template, spec.container)
    if container is None:
        return ExpectedRelation(result="missing", limitations=("container_missing",))
    cm = maps.get(spec.config_map, {})
    data = cm.get("data") or {}
    if spec.key not in data:
        if spec.key in (cm.get("binaryDataKeys") or ()):
            return ExpectedRelation(result="unsupported", limitations=("binary_key",))
        return ExpectedRelation(result="missing", limitations=("config_map_key_missing",))
    binding = _binding(container, spec, maps, secrets)
    if binding != "matched":
        return ExpectedRelation(result=binding, limitations=("env_binding",))
    if spec.expected_service not in services:
        return ExpectedRelation(result="missing", limitations=("expected_service_missing",))
    return _destination(config, spec, snapshot.namespace, data[spec.key], services)


def _binding(
    container: Obj, spec: ServiceConfigBaseline, maps: dict[str, Obj], secrets: dict[str, Obj]
) -> Literal["matched", "mismatch", "missing", "unsupported"]:
    for env in reversed(container.get("env") or []):
        if env["name"] != spec.env:
            continue
        value_from = env.get("valueFrom") or {}
        if ref := value_from.get("configMapKeyRef"):
            name, key = ref.get("name"), ref.get("key")
            if key not in (maps.get(name, {}).get("data") or {}):
                if ref.get("optional"):
                    continue
                return "missing"
            return "matched" if (name, key) == (spec.config_map, spec.key) else "mismatch"
        if (
            (ref := value_from.get("secretKeyRef"))
            and ref.get("optional")
            and ref.get("key") not in (secrets.get(ref.get("name"), {}).get("keys") or [])
        ):
            continue
        return "unsupported"
    # envFrom의 뒤쪽 값이 앞선 값을 덮는다. Secret은 키 이름만 확인한다.
    for source in reversed(container.get("envFrom") or []):
        prefix = source.get("prefix") or ""
        if not spec.env.startswith(prefix):
            continue
        key = spec.env[len(prefix) :]
        if ref := source.get("secretRef"):
            obj = secrets.get(ref.get("name"))
            if obj is None and not ref.get("optional") or key in (obj or {}).get("keys", []):
                return "unsupported"
        elif ref := source.get("configMapRef"):
            name = ref.get("name")
            if name not in maps and not ref.get("optional"):
                return "unsupported"
            if key in (maps.get(name, {}).get("data") or {}):
                return "matched" if (name, key) == (spec.config_map, spec.key) else "mismatch"
    return "missing"


def _destination(
    config: KubeGuardianConfig,
    spec: ServiceConfigBaseline,
    namespace: str,
    value: str,
    services: dict[str, Obj],
) -> ExpectedRelation:
    try:
        netloc = urlsplit(value if "://" in value else f"//{value}").netloc
        if netloc.rpartition("@")[2].endswith(":***"):
            return ExpectedRelation(result="unsupported", limitations=("service_port_masked",))
        edge = _edge(
            "",
            spec.env,
            value,
            namespace,
            {n: _service_ports(o) for n, o in services.items()},
            config.external_hosts,
            set(),
        )
    except ValueError:
        edge = None
    if edge is None or edge.port is None:
        return ExpectedRelation(result="unsupported", limitations=("service_endpoint_unreadable",))
    if edge.status != "declared":
        return ExpectedRelation(result="mismatch", limitations=("service_endpoint_unresolved",))
    return ExpectedRelation(
        result="matched" if edge.target == spec.expected_service else "mismatch",
        observed_service=edge.target,
    )
