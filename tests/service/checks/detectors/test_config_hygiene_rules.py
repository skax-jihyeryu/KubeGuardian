"""C01·H01·H02, 그리고 모든 규칙의 수집 실패 → 확인하지 못함 (05-rules §4, §7)."""

from datetime import timedelta

import pytest

from core.config import KubeGuardianConfig
from service.checks.detectors.catalog import CATALOG
from service.sensors.collector import collect
from tests.service.checks.detectors.cases import (
    CONFIG,
    NOW,
    FixtureReader,
    context,
    failed_snapshot,
    one,
    raw_cluster,
    run,
    snapshot,
)

X2K, Q8W = "dev/Pod/agent-7c9f5d6b4-x2k9p", "dev/Pod/agent-7c9f5d6b4-q8w3z"


# ── C01 설정 참조 누락 ──


def test_c01_passes_when_references_exist():
    s = one(run("C01", context(snapshot())), X2K)
    assert s.verdict == "passed"
    kinds = {(r["kind"], r["name"], r.get("key")) for r in s.values["references"]}
    assert kinds == {
        ("ConfigMap", "agent-config", None),
        ("Secret", "agent-secret", "db-password"),
    }
    assert all(r["exists"] for r in s.values["references"])


def test_c01_detects_missing_objects_and_keys_as_critical_without_ready_pods():
    signals = run("C01", context(snapshot("c01/detected")))
    env_pod, vol_pod = one(signals, X2K), one(signals, Q8W)
    assert (env_pod.verdict, env_pod.severity) == ("detected", "critical")
    missing = {(r["kind"], r["name"], r.get("key")) for r in env_pod.values["missing"]}
    assert missing == {
        ("ConfigMap", "agent-config", "MODEL_NAME"),
        ("Secret", "agent-token", "token"),
    }
    assert {(r["kind"], r["name"], r.get("key")) for r in vol_pod.values["missing"]} == {
        ("ConfigMap", "agent-config", "missing.txt"),
        ("Secret", "agent-tls", None),
    }
    where = {r["where"] for r in vol_pod.values["missing"]}
    assert where == {"volume prompts", "volume certs"}


def test_c01_warns_when_other_pods_of_deployment_are_ready():
    s = one(run("C01", context(snapshot("c01/old_ready"))), X2K)
    assert (s.verdict, s.severity) == ("detected", "warning")
    assert s.values["missing"][0]["where"] == "container agent envFrom"


def test_c01_excludes_optional_references():
    s = one(run("C01", context(snapshot("c01/optional"))), X2K)
    assert s.verdict == "passed"


# ── H01 readinessProbe 없음·TCP만 ──


def test_h01_passes_http_probe():
    s = one(run("H01", context(snapshot())), "dev/Deployment/agent/agent")
    assert s.verdict == "passed"
    assert s.values["probe"] == "httpGet"


def test_h01_advises_tcp_only_and_missing_probe_once_per_workload():
    signals = run("H01", context(snapshot("h01/detected")))
    agent = one(signals, "dev/Deployment/agent/agent")  # replicas 2지만 한 번만
    gateway = one(signals, "dev/Deployment/gateway/gateway")
    assert (agent.verdict, agent.severity, agent.values["probe"]) == (
        "detected",
        "info",
        "tcpSocket",
    )
    assert (gateway.verdict, gateway.severity, gateway.values["probe"]) == (
        "detected",
        "info",
        None,
    )


def test_h01_excludes_pods_not_selected_by_a_service():
    signals = run("H01", context(snapshot("h01/excluded")))
    assert all("migrate" not in s.subject for s in signals)


# ── H02 requests·limits 미설정 ──


def test_h02_passes_full_resources():
    assert (
        one(run("H02", context(snapshot())), "dev/Deployment/gateway/gateway").verdict == "passed"
    )


def test_h02_advises_missing_resources():
    s = one(run("H02", context(snapshot("h02/detected"))), "dev/Deployment/gateway/gateway")
    assert (s.verdict, s.severity) == ("detected", "info")
    assert s.values["missing"] == ["requests.memory", "limits.cpu", "limits.memory"]
    assert s.values["resources"] == {"requests": {"cpu": "100m"}}


# ── 수집 실패 → 확인하지 못함 ──

INPUTS = {
    "N01": "Node",
    "P03": "Pod",
    "P06": "Pod",
    "P07": "Pod",
    "W01": "Deployment",
    "W02": "Deployment",
    "S01": "Service",
    "S02": "EndpointSlice",
    "S03": "Pod",
    "S04": "Service",
    "C01": "Secret",
    "C05": "ConfigMap",
    "H01": "Service",
    "H02": "Pod",
}


def test_every_rule_has_a_collection_failure_case():
    assert set(INPUTS) == set(CATALOG)


@pytest.mark.parametrize(("rule_id", "kind"), sorted(INPUTS.items()))
def test_collection_failure_is_unknown_not_passed(rule_id, kind, obs):
    config = REQUIRED_CONFIG_CTX if rule_id == "C05" else CONFIG
    signals = run(rule_id, context(failed_snapshot(kind), observations=obs, config=config))
    assert [(s.verdict, s.subject) for s in signals] == [("unknown", "dev")]
    assert kind in signals[0].values["failed_kinds"]
    assert obs.rows == {}


# ── Service·ReplicaSet 수집 실패: 묶음만 바꿔 판정을 계속한다 (05-rules §0.3) ──

# 규칙 → 수집에 실패해도 판정을 계속하는 묶음용 kind
DEGRADABLE = {
    "P03": ("Service", "ReplicaSet"),
    "P06": ("Service", "ReplicaSet"),
    "P07": ("Service", "ReplicaSet"),
    "C01": ("Service", "ReplicaSet"),
    "H02": ("Service", "ReplicaSet"),
    "W01": ("Service",),
    "W02": ("Service",),
}


def _degraded_snapshot(case, *kinds):
    objects, _ = raw_cluster(case)
    return collect(FixtureReader(objects, {k: "403 Forbidden" for k in kinds}), now=NOW)


@pytest.mark.parametrize(
    ("rule_id", "kind"), [(r, k) for r, kinds in sorted(DEGRADABLE.items()) for k in kinds]
)
def test_grouping_kind_failure_keeps_judging_and_records_it(rule_id, kind, obs):
    signals = run(rule_id, context(failed_snapshot(kind), observations=obs))

    assert signals
    assert all(s.verdict != "unknown" for s in signals)
    assert all(s.values["failed_kinds"] == {kind: "403 Forbidden"} for s in signals)


@pytest.mark.parametrize("kinds", [("Service",), ("Service", "ReplicaSet")])
def test_degraded_finding_groups_by_deployment_and_caps_severity_at_warning(kinds, obs):
    for pod in ("agent-7c9f5d6b4-x2k9p", "agent-7c9f5d6b4-q8w3z"):
        obs.seed("P03", f"dev/Pod/{pod}/agent", NOW - timedelta(hours=2))

    signals = run("P03", context(_degraded_snapshot("p03/all_failing", *kinds), observations=obs))

    agent = [s for s in signals if s.service == "agent"]  # Deployment 이름으로 묶음
    assert {(s.verdict, s.severity) for s in agent} == {("detected", "warning")}  # 원래 긴급
    assert all(s.values["ready_pods_in_deployment"] == 0 for s in agent)
    assert {s.service for s in signals} == {"agent", "gateway"}


def test_replicaset_only_failure_keeps_critical(obs):
    """Service를 읽었으면 서비스 이름이 정확하므로 심각도를 낮추지 않는다."""
    for pod in ("agent-7c9f5d6b4-x2k9p", "agent-7c9f5d6b4-q8w3z"):
        obs.seed("P03", f"dev/Pod/{pod}/agent", NOW - timedelta(hours=2))

    signals = run(
        "P03", context(_degraded_snapshot("p03/all_failing", "ReplicaSet"), observations=obs)
    )

    agent = [s for s in signals if s.service == "agent"]
    assert {(s.verdict, s.severity) for s in agent} == {("detected", "critical")}
    assert all(s.values["failed_kinds"] == {"ReplicaSet": "403 Forbidden"} for s in agent)


def test_replicaset_failure_still_groups_hygiene_advice_by_deployment():
    signals = run("H02", context(failed_snapshot("ReplicaSet")))

    assert {s.subject for s in signals} == {
        "dev/Deployment/gateway/gateway",
        "dev/Deployment/agent/agent",
    }


@pytest.mark.parametrize(("rule_id", "kind"), [("H01", "Service"), ("W02", "ReplicaSet")])
def test_kind_needed_for_the_condition_itself_stays_unknown(rule_id, kind):
    signals = run(rule_id, context(failed_snapshot(kind)))

    assert [s.verdict for s in signals] == ["unknown"]


# ── C05 필수 설정 누락 (required_config) ──


def _config(required, core_paths=None):
    return KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "core_paths": core_paths if core_paths is not None else {"chat": ["gateway", "agent"]},
            "required_config": required,
        }
    )


REQUIRED_CONFIG_CTX = _config({"gateway": ["ADMIN_SERVICE_URL"]})


def _c05(required, mutate=None, core_paths=None):
    objects, failed = raw_cluster()
    if mutate:
        mutate(objects)
    snap = collect(FixtureReader(objects, failed), now=NOW)
    return run("C05", context(snap, config=_config(required, core_paths)))


def _containers(objects, deployment):
    dep = next(d for d in objects["Deployment"] if d["metadata"]["name"] == deployment)
    return dep["spec"]["template"]["spec"]["containers"]


def _verdict(signals, service="gateway"):
    (s,) = [s for s in signals if s.service == service]
    return s


def test_required_config_empty_judges_nothing():
    assert _c05({}) == []


def test_missing_required_env_is_detected():
    s = _verdict(_c05({"gateway": ["ADMIN_SERVICE_URL"]}))
    assert (s.verdict, s.rule_id, s.subject) == ("detected", "C05", "dev/Service/gateway")
    assert s.values["missing"] == ["ADMIN_SERVICE_URL"]
    assert s.values["required"] == ["ADMIN_SERVICE_URL"]


def test_present_in_env_passes():
    s = _verdict(_c05({"gateway": ["AGENT_SERVICE_URL"]}))
    assert s.verdict == "passed"
    assert s.values["found"] == {"AGENT_SERVICE_URL": "env"}
    assert s.values["missing"] == []


def test_present_via_configmap_envfrom_passes():
    s = _verdict(_c05({"agent": ["LOG_LEVEL"]}), "agent")
    assert s.verdict == "passed"
    assert s.values["found"] == {"LOG_LEVEL": "configmap:agent-config"}


def test_present_via_secret_envfrom_key_name_passes():
    def mutate(o):
        _containers(o, "gateway")[0]["envFrom"] = [{"secretRef": {"name": "agent-secret"}}]

    s = _verdict(_c05({"gateway": ["db-password"]}, mutate))
    assert s.verdict == "passed"
    assert s.values["found"] == {"db-password": "secret:agent-secret"}


def test_envfrom_prefix_applies():
    def mutate(o):
        _containers(o, "gateway")[0]["envFrom"] = [
            {"prefix": "APP_", "configMapRef": {"name": "agent-config"}}
        ]

    assert _verdict(_c05({"gateway": ["APP_LOG_LEVEL"]}, mutate)).verdict == "passed"
    assert _verdict(_c05({"gateway": ["LOG_LEVEL"]}, mutate)).verdict == "detected"


def test_valuefrom_and_empty_value_count_as_present():
    s = _verdict(_c05({"agent": ["DB_PASSWORD"]}), "agent")
    assert s.values["found"] == {"DB_PASSWORD": "valueFrom"}

    def mutate(o):
        _containers(o, "gateway")[0]["env"].append({"name": "EMPTY", "value": ""})

    assert _verdict(_c05({"gateway": ["EMPTY"]}, mutate)).verdict == "passed"


def test_multi_container_any_container_satisfies():
    def mutate(o):
        _containers(o, "gateway").append({"name": "side", "env": [{"name": "X", "value": "1"}]})

    assert _verdict(_c05({"gateway": ["X"]}, mutate)).verdict == "passed"


def test_declared_service_missing_is_detected():
    (s,) = _c05({"gatway": ["X"]})
    assert (s.verdict, s.service) == ("detected", "gatway")
    assert s.values["error"] == "선언한 서비스 없음"


def test_missing_referenced_configmap_is_unknown_not_detected():
    def mutate(o):
        o["ConfigMap"] = []

    s = _verdict(_c05({"agent": ["LOG_LEVEL"]}, mutate), "agent")
    assert s.verdict == "unknown"
    assert s.values["unchecked"] == ["LOG_LEVEL"]


def test_optional_missing_configmap_does_not_hide_missing_name():
    def mutate(o):
        o["ConfigMap"] = []
        _containers(o, "agent")[0]["envFrom"][0]["configMapRef"]["optional"] = True

    assert _verdict(_c05({"agent": ["LOG_LEVEL"]}, mutate), "agent").verdict == "detected"


def test_severity_follows_core_paths():
    critical = _verdict(_c05({"gateway": ["X"]}))
    other = _verdict(_c05({"gateway": ["X"]}, core_paths={"chat": ["agent"]}))
    assert (critical.severity, other.severity) == ("critical", "warning")


def test_service_collection_failure_is_unknown_not_detected():
    signals = run("C05", context(failed_snapshot("Service"), config=_config({"gatway": ["X"]})))

    assert [(s.verdict, s.subject) for s in signals] == [("unknown", "dev")]
    assert "Service" in signals[0].values["failed_kinds"]


def test_missing_ref_with_other_prefix_does_not_hide_missing_name():
    def mutate(o):
        o["ConfigMap"] = []
        _containers(o, "agent")[0]["envFrom"][0]["prefix"] = "APP_"

    s = _verdict(_c05({"agent": ["LOG_LEVEL"]}, mutate), "agent")
    assert (s.verdict, s.values["missing"]) == ("detected", ["LOG_LEVEL"])


def test_detected_wins_over_unchecked_in_same_service():
    def mutate(o):
        o["ConfigMap"] = []
        _containers(o, "agent")[0]["envFrom"][0]["prefix"] = "APP_"

    s = _verdict(_c05({"agent": ["APP_LOG_LEVEL", "NOPE"]}, mutate), "agent")
    assert (s.verdict, s.values["missing"], s.values["unchecked"]) == (
        "detected",
        ["NOPE"],
        ["APP_LOG_LEVEL"],
    )
