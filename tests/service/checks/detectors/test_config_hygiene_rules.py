"""C01·H01·H02, 그리고 모든 규칙의 수집 실패 → 확인하지 못함 (05-rules §4, §7)."""

from datetime import timedelta

import pytest

from service.checks.detectors.catalog import CATALOG
from service.sensors.collector import collect
from tests.service.checks.detectors.cases import (
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
    "H01": "Service",
    "H02": "Pod",
}


def test_every_rule_has_a_collection_failure_case():
    assert set(INPUTS) == set(CATALOG)


@pytest.mark.parametrize(("rule_id", "kind"), sorted(INPUTS.items()))
def test_collection_failure_is_unknown_not_passed(rule_id, kind, obs):
    signals = run(rule_id, context(failed_snapshot(kind), observations=obs))
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
