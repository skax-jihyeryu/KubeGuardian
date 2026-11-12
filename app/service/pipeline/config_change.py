"""Persist and cite scoped configuration comparisons in checks and investigations."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import CheckResultStore, CheckRunStore, EvidenceStore
from core.guard.redact import redact_mapping
from core.log.logging import get_logging
from service.config_baseline import _scenarios
from service.config_change import (
    CONFIG_CHANGE_SOURCE,
    compare_config_change,
    has_current_intent,
    observation_time,
)
from service.model import Evidence

logger = get_logging()
_LOG_LIMIT = 20


def record_config_changes(
    engine: Engine, config: KubeGuardianConfig, run_id: int, observed_at: datetime
) -> None:
    if not config.config_baselines:
        return
    runs = CheckRunStore(engine)
    store = EvidenceStore(engine, run_id)
    try:
        check = CheckResultStore(engine).get(run_id)
        if check is None or check.status != "done" or check.finished_at is None:
            return
        existing = {
            ev.data.get("consumer", {}).get("service")
            for ev in store.list_for_run(
                run_id, limit=len(config.config_baselines), source=CONFIG_CHANGE_SOURCE
            )
        }
        for service, spec in sorted(config.config_baselines.items()):
            if service in existing:
                continue
            result = compare_config_change(
                config, service, current_run_id=run_id, load_check=runs.get_pinned_snapshot
            )
            result["current"]["started_at"] = check.started_at.isoformat()
            result["current"]["finished_at"] = check.finished_at.isoformat()
            result["symptoms"] = []
            if result.get("interval"):
                collected_at = observation_time(result["current"]["collected_at"])
                assert collected_at is not None
                scenario_names = {scenario.name for scenario in _scenarios(config, service)}
                for symptom in check.symptoms:
                    scenario = symptom.get("scenario")
                    ev_id = symptom.get("evidence_id")
                    if scenario not in scenario_names or not isinstance(ev_id, str):
                        continue
                    if store.check_run_of(ev_id) != run_id or (ev := store.get(ev_id)) is None:
                        continue
                    if (
                        ev.kind != "app_api"
                        or ev.subject != f"{config.target.namespace}/Scenario/{scenario}"
                        or ev.data.get("scenario") != scenario
                        or not collected_at <= ev.collected_at <= check.finished_at
                        or "failed_step" not in ev.data
                        or symptom.get("failed_step") != ev.data["failed_step"]
                        or symptom.get("status")
                        != ("passed" if ev.data["failed_step"] is None else "failed")
                    ):
                        continue
                    result["symptoms"].append(
                        {
                            "scenario": scenario,
                            "status": symptom["status"],
                            "observed_at": ev.collected_at.isoformat(),
                            "evidence_id": ev_id,
                        }
                    )
            store.add(
                Evidence(
                    kind="resource",
                    subject=f"{config.target.namespace}/Deployment/{spec.deployment}",
                    source=CONFIG_CHANGE_SOURCE,
                    collected_at=observed_at,
                    data=redact_mapping(result),
                )
            )
    except Exception as exc:  # A comparison failure must never fail the check or log its input.
        logger.warning(f"설정 변경 근거 기록 실패: {type(exc).__name__}")


def investigation_comparison(
    engine: Engine, config: KubeGuardianConfig, run_id: int
) -> Callable[[str], dict[str, Any]]:
    store = EvidenceStore(engine, run_id)
    runs = CheckRunStore(engine)

    def load(service: str) -> dict[str, Any]:
        try:
            spec = config.config_baselines[service]
            current, done = runs.get_pinned_snapshot(run_id)
            if current is None or not done or not has_current_intent(config, service, current):
                return {"status": "unknown", "limitations": ["comparison_scope_unverified"]}
            subject = f"{config.target.namespace}/Deployment/{spec.deployment}"
            for ev in store.list_for_run(
                run_id, limit=len(config.config_baselines), source=CONFIG_CHANGE_SOURCE
            ):
                if (
                    ev.subject == subject
                    and ev.kind == "resource"
                    and ev.data.get("consumer", {}).get("service") == service
                    and ev.data.get("baseline", {}).get("run_id") == spec.run_id
                ):
                    result = {**ev.data, "evidence_id": ev.id}
                    result["logs"] = _related_logs(store, run_id, result, config.target.namespace)
                    return result
        except Exception as exc:  # Never send comparison internals through Tool's error path.
            logger.warning(f"설정 변경 근거 읽기 실패: {type(exc).__name__}")
        return {"status": "unknown", "limitations": ["comparison_unavailable"]}

    return load


def _related_logs(
    store: EvidenceStore, run_id: int, result: dict[str, Any], namespace: str
) -> list[dict[str, Any]]:
    interval = result.get("interval")
    if not interval:
        return []
    start = observation_time(interval["baseline_collected_at"])
    finish = observation_time(result["current"]["finished_at"])
    assert start is not None and finish is not None
    pods = {pod["name"]: pod for pod in result["owned_pods"].get("current", [])}
    if not pods:
        return []
    logs = []
    for ev in store.list_for_run(run_id, limit=100, kind="log_excerpt"):
        pod = pods.get(ev.subject.removeprefix(f"{namespace}/Pod/"))
        if (
            pod is None
            or ev.subject != f"{namespace}/Pod/{pod['name']}"
            or ev.data.get("pod_uid") != pod["uid"]
            or ev.data.get("container") != result["consumer"]["container"]
        ):
            continue
        first = observation_time(ev.data.get("first_time"))
        last = observation_time(ev.data.get("last_time"))
        created = observation_time(pod["created_at"])
        if (
            first is None
            or last is None
            or created is None
            or first < created
            or not start < first <= last <= min(finish, ev.collected_at)
        ):
            continue
        logs.append(
            {
                "pod": pod["name"],
                "first_event_at": first.isoformat(),
                "last_event_at": last.isoformat(),
                "observed_at": ev.collected_at.isoformat(),
                "evidence_id": ev.id,
            }
        )
        if len(logs) == _LOG_LIMIT:
            break
    return logs
