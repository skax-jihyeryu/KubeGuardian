"""Compare observations of a configured ConfigMap env dependency (#180)."""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from core.config import KubeGuardianConfig, ServiceConfigBaseline
from core.guard.redact import MASK, is_sensitive_name
from service.config_baseline import (
    Obj,
    PinnedLoader,
    _container,
    _intent,
    _objects_by_name,
    _pinned,
    evaluate_relation,
)
from service.model import ClusterSnapshot

CONFIG_CHANGE_SOURCE = "check_config_change"
_LIST_LIMIT = 20
_ENV_NAME = re.compile(r"[-._A-Za-z0-9]+")

Change = Literal["changed", "no_change", "unknown"]


def compare_config_change(
    config: KubeGuardianConfig,
    service: str,
    *,
    current_run_id: int,
    load_check: PinnedLoader,
) -> dict[str, Any]:
    spec = config.config_baselines[service]
    current, current_done = load_check(current_run_id)
    baseline, baseline_done = (
        load_check(spec.run_id)
        if spec.run_id is not None and spec.run_id < current_run_id
        else (None, False)
    )
    pinned = _pinned(
        config,
        service,
        spec,
        baseline,
        baseline_done,
        current.collected_at if current else datetime.now(UTC),
        current_run_id,
    )
    result: dict[str, Any] = {
        "status": "unknown",
        "consumer": {
            "service": service,
            "deployment": spec.deployment,
            "container": spec.container,
        },
        "config_map": spec.config_map,
        "baseline": pinned.model_dump(mode="json", exclude_unset=True),
        "current": {
            "run_id": current_run_id,
            "collected_at": current.collected_at.isoformat() if current else None,
        },
        "key_presence": {"status": "unknown"},
        "key_value": "unknown",
        "deployment_env": "unknown",
        "owned_pods": {"status": "unknown"},
        "env_application": "unverified",
        "volume_and_application_reload": "unverified",
        "limitations": [],
    }
    for field, name in (("env", spec.env), ("key", spec.key)):
        if not is_sensitive_name(name):
            result[field] = name

    limitations = result["limitations"]
    if current is None or not current_done:
        limitations.append("current_check_incomplete")
    elif (
        current.namespace != config.target.namespace
        or current.config_baseline is None
        or current.config_baseline.project_id != config.project.id
    ):
        limitations.append("current_target_unverified")
    elif not has_current_intent(config, service, current):
        limitations.append("current_intent_mismatch")
    if pinned.result != "approved" or limitations or current is None or baseline is None:
        if pinned.result != "approved":
            result["baseline"].pop("collected_at", None)
        return result

    result["interval"] = {
        "baseline_collected_at": baseline.collected_at.isoformat(),
        "current_collected_at": current.collected_at.isoformat(),
    }
    before_key, before_value = _key(baseline, spec)
    after_key, after_value = _key(current, spec)
    result["key_presence"] = {
        "status": _compare(before_key, after_key),
        "baseline": before_key,
        "current": after_key,
    }
    result["key_value"] = _compare(before_value, after_value)
    relation = evaluate_relation(config, current, service, spec)
    result["observed_binding"] = {
        "status": relation.result,
        "limitations": list(relation.limitations),
    }
    before_consumers, after_consumers = _consumers(baseline, spec), _consumers(current, spec)
    result["declared_consumers"] = {
        "baseline": before_consumers[:_LIST_LIMIT] if before_consumers is not None else None,
        "current": after_consumers[:_LIST_LIMIT] if after_consumers is not None else None,
        "truncated": max(len(before_consumers or ()), len(after_consumers or ())) > _LIST_LIMIT,
    }
    if after_key is None:
        limitations.append("config_map_collection_incomplete")
    elif after_key == "binary":
        limitations.append("binary_value_unavailable")
    elif after_key == before_key and after_value is None:
        limitations.append("value_unavailable_or_redacted")

    before_binding, after_binding = _binding(baseline, spec), _binding(current, spec)
    result["deployment_env"] = _compare(before_binding, after_binding)
    if result["deployment_env"] == "unknown":
        limitations.append("deployment_env_unverified")

    before_pods, after_pods = _owned_pods(baseline, spec), _owned_pods(current, spec)
    if before_pods is None or after_pods is None:
        limitations.append("pod_ownership_unverified")
    else:
        added = [pod for uid, pod in sorted(after_pods.items()) if uid not in before_pods]
        removed = [pod for uid, pod in sorted(before_pods.items()) if uid not in after_pods]
        created = [
            pod
            for pod in added
            if (timestamp := observation_time(pod["created_at"])) is not None
            and baseline.collected_at < timestamp <= current.collected_at
        ]
        result["owned_pods"] = {
            "status": _compare(before_pods.keys(), after_pods.keys()),
            "created": created[:_LIST_LIMIT],
            "added": added[:_LIST_LIMIT],
            "removed": removed[:_LIST_LIMIT],
            "current": [after_pods[uid] for uid in sorted(after_pods)[:_LIST_LIMIT]],
            "truncated": max(len(added), len(removed), len(after_pods)) > _LIST_LIMIT,
        }

    statuses = (
        result["key_presence"]["status"],
        result["key_value"],
        result["deployment_env"],
        result["owned_pods"]["status"],
    )
    if "changed" in statuses:
        result["status"] = "changed"
    elif "unknown" not in statuses:
        result["status"] = "no_change"
    return result


def has_current_intent(config: KubeGuardianConfig, service: str, snapshot: ClusterSnapshot) -> bool:
    provenance = snapshot.config_baseline
    if (
        snapshot.namespace != config.target.namespace
        or provenance is None
        or provenance.project_id != config.project.id
    ):
        return False
    observation = provenance.observations.get(service)
    return observation is not None and observation.intent_digest == _intent(
        config, service, config.config_baselines[service]
    )


def _compare(before: Any, after: Any) -> Change:
    if before is None or after is None:
        return "unknown"
    return "no_change" if before == after else "changed"


def _key(snapshot: ClusterSnapshot, spec: ServiceConfigBaseline) -> tuple[str | None, str | None]:
    if snapshot.items("ConfigMap") is None:
        return None, None
    cm = _objects_by_name(snapshot, "ConfigMap").get(spec.config_map, {})
    if spec.key in (cm.get("binaryDataKeys") or []):
        return "binary", None
    data = cm.get("data") or {}
    if spec.key not in data:
        return "missing", None
    value = data[spec.key]
    comparable = not is_sensitive_name(spec.key) and isinstance(value, str) and MASK not in value
    return "present", value if comparable else None


def _binding(snapshot: ClusterSnapshot, spec: ServiceConfigBaseline) -> tuple | None:
    """Compare the exact env declaration locally, without returning declaration values."""
    if snapshot.items("Deployment") is None:
        return None
    deployment = _objects_by_name(snapshot, "Deployment").get(spec.deployment)
    if deployment is None:
        return ("deployment_missing",)
    uid = deployment["metadata"].get("uid")
    if not uid:
        return None
    template = deployment["spec"].get("template") or {}
    container = _container(template, spec.container)
    if container is None:
        return uid, "container_missing"
    direct = [env for env in container.get("env") or [] if env["name"] == spec.env]
    sources = [
        source
        for source in container.get("envFrom") or []
        if spec.env.startswith(source.get("prefix") or "")
    ]

    # An otherwise identical literal can contain masked portions. Compare declarations
    # of references only, while still detecting that the source type was changed.
    def binding_type(env: Obj) -> Obj:
        return env.get("valueFrom") or {"literal": True}

    return uid, "container_present", [binding_type(env) for env in direct], sources


def _consumers(snapshot: ClusterSnapshot, spec: ServiceConfigBaseline) -> list[Obj] | None:
    deployments = snapshot.items("Deployment")
    if deployments is None:
        return None
    refs = set()
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        template = deployment["spec"].get("template") or {}
        for container in (template.get("spec") or {}).get("containers") or []:
            for env in container.get("env") or []:
                ref = (env.get("valueFrom") or {}).get("configMapKeyRef") or {}
                if (ref.get("name"), ref.get("key")) == (spec.config_map, spec.key):
                    refs.add((name, container["name"], env["name"], "env"))
            for source in container.get("envFrom") or []:
                if (source.get("configMapRef") or {}).get("name") == spec.config_map:
                    env_name = (source.get("prefix") or "") + spec.key
                    refs.add((name, container["name"], env_name, "envFrom"))
    results = []
    for deployment_name, container_name, env_name, source in sorted(refs):
        item = {"deployment": deployment_name, "container": container_name, "source": source}
        if not is_sensitive_name(env_name) and _ENV_NAME.fullmatch(env_name):
            item["env"] = env_name
        results.append(item)
    return results


def _owner_ref(obj: Obj, kind: str) -> Obj:
    return next(
        (
            ref
            for ref in obj["metadata"].get("ownerReferences") or []
            if ref.get("kind") == kind and ref.get("controller") is not False
        ),
        {},
    )


def _owned_pods(
    snapshot: ClusterSnapshot, spec: ServiceConfigBaseline
) -> dict[str, dict[str, str]] | None:
    if any(snapshot.items(kind) is None for kind in ("Deployment", "ReplicaSet", "Pod")):
        return None
    deployment = _objects_by_name(snapshot, "Deployment").get(spec.deployment)
    if deployment is None:
        return {}
    deployment_uid = deployment["metadata"].get("uid")
    if not deployment_uid:
        return None
    replicas = {}
    for rs in snapshot.items("ReplicaSet") or []:
        owner = _owner_ref(rs, "Deployment")
        if owner.get("name") != spec.deployment:
            continue
        if not owner.get("uid") or owner.get("controller") is not True:
            return None
        if owner["uid"] == deployment_uid:
            if not rs["metadata"].get("uid"):
                return None
            replicas[rs["metadata"]["name"]] = rs["metadata"]["uid"]
    pods = {}
    for pod in snapshot.items("Pod") or []:
        owner = _owner_ref(pod, "ReplicaSet")
        replica_uid = replicas.get(owner.get("name"))
        if replica_uid is None:
            continue
        if not owner.get("uid") or owner.get("controller") is not True:
            return None
        if owner["uid"] != replica_uid:
            continue
        meta = pod["metadata"]
        created_at = observation_time(meta.get("creationTimestamp"))
        if not meta.get("uid") or created_at is None or created_at > snapshot.collected_at:
            return None
        pods[meta["uid"]] = {
            "name": meta["name"],
            "uid": meta["uid"],
            "created_at": created_at.isoformat(),
        }
    return pods


def observation_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return observed if observed.utcoffset() is not None else None
