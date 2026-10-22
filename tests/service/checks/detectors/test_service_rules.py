"""S01·S02·S03·S04 (05-rules §4, §7)."""

from datetime import timedelta

import pytest

from service.model import Topology, TopologyEdge
from service.sensors.collector import collect
from tests.service.checks.detectors.cases import (
    NOW,
    FixtureReader,
    context,
    one,
    raw_cluster,
    run,
    snapshot,
)

AGENT_SVC = "dev/Service/agent"
GW_SVC = "dev/Service/gateway"


# ── S01 selector에 맞는 Pod 없음 ──


def test_s01_passes_when_selector_matches_pods():
    s = one(run("S01", context(snapshot())), AGENT_SVC)
    assert s.verdict == "passed"
    assert s.values["matched_pods"] == 2


def test_s01_detects_selector_typo_immediately_as_critical_on_core_path():
    s = one(run("S01", context(snapshot("s01/detected"))), AGENT_SVC)
    assert (s.verdict, s.severity, s.service) == ("detected", "critical", "agent")
    assert s.values["selector"] == {"app": "agnet"}
    # 비교용: selector 키가 같은 Pod 라벨
    assert {"app": "agent", "pod-template-hash": "7c9f5d6b4"} in s.values["similar_pod_labels"]


def test_s01_is_warning_off_core_path():
    from core.config import KubeGuardianConfig

    config = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}})
    s = one(run("S01", context(snapshot("s01/detected"), config=config)), AGENT_SVC)
    assert s.severity == "warning"


def test_s01_excludes_services_without_selector():
    signals = run("S01", context(snapshot("s01/excluded")))
    for name in ("openai", "legacy-db"):
        s = one(signals, f"dev/Service/{name}")
        assert s.verdict == "passed"
        assert s.values["excluded"] == "selector 없음"


# ── S02 Ready 엔드포인트 없음·일부 ──


def test_s02_passes_with_all_endpoints_ready():
    s = one(run("S02", context(snapshot())), AGENT_SVC)
    assert s.verdict == "passed"
    assert (s.values["ready"], s.values["total"]) == (2, 2)


def test_s02_partial_holds_15_minutes_then_warns(obs):
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=14))
    assert one(
        run("S02", context(snapshot("s02/partial"), observations=obs)), AGENT_SVC
    ).verdict == ("observing")
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(snapshot("s02/partial"), observations=obs)), AGENT_SVC)
    assert (s.verdict, s.severity) == ("detected", "warning")
    assert (s.values["ready"], s.values["total"]) == (1, 2)
    assert s.values["not_ready"] == [
        {
            "pod": "agent-7c9f5d6b4-q8w3z",
            "ready_condition": {
                "status": "False",
                "reason": "ContainersNotReady",
                "message": "containers with unready status: [agent]",
            },
        }
    ]


def test_s02_none_is_critical_on_core_path(obs):
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(snapshot("s02/none"), observations=obs)), AGENT_SVC)
    assert (s.verdict, s.severity) == ("detected", "critical")


def test_s02_ignores_terminating_endpoints():
    s = one(run("S02", context(snapshot("s02/terminating"))), AGENT_SVC)
    assert s.verdict == "passed"
    assert (s.values["ready"], s.values["total"]) == (2, 2)


def test_s02_leaves_selector_typo_to_s01(obs):
    s = one(run("S02", context(snapshot("s01/detected"), observations=obs)), AGENT_SVC)
    assert s.verdict == "passed"
    assert s.values["excluded"] == "대상 Pod 0개 (S01)"


NEW_REVISION_POD = "agent-5f8d7c6b9-n7v2c"


def test_s02_excludes_new_revision_pod_while_old_revision_serves(obs):
    # A3: 새 ReplicaSet Pod 1개가 ErrImagePull, 이전 버전 Pod 3개는 Ready
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=30))
    s = one(run("S02", context(snapshot("s02/rollout"), observations=obs)), AGENT_SVC)
    assert s.verdict == "passed"
    assert (s.values["ready"], s.values["total"]) == (3, 3)
    assert s.values["rollout_excluded"] == [NEW_REVISION_POD]
    assert obs.get("S02", AGENT_SVC) is None


def test_s02_still_detects_not_ready_pod_of_only_revision(obs):
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(snapshot("s02/partial"), observations=obs)), AGENT_SVC)
    assert s.verdict == "detected"
    assert s.values["rollout_excluded"] == []


def test_s02_does_not_exclude_new_revision_when_old_revision_is_not_ready(obs):
    def mutate(objects):
        for ep in _by_name(objects, "EndpointSlice", "agent-m4q9t")["endpoints"]:
            ep["conditions"] = {"ready": False, "serving": False, "terminating": False}

    objects, _ = raw_cluster("s02/rollout")
    mutate(objects)
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(_snapshot_of(objects), observations=obs)), AGENT_SVC)
    assert (s.verdict, s.values["ready"], s.values["total"]) == ("detected", 0, 4)
    assert s.values["rollout_excluded"] == []


def test_s02_skips_rollout_exclusion_when_replicasets_unreadable(obs):
    # 새 버전인지 알 수 없으므로 제외하지 않는다 (05-rules S02 제외, §0.3)
    objects, _ = raw_cluster("s02/rollout")
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    snap = _snapshot_of(objects, {"ReplicaSet": "403 Forbidden"})
    s = one(run("S02", context(snap, observations=obs)), AGENT_SVC)
    assert (s.verdict, s.severity) == ("detected", "warning")
    assert (s.values["ready"], s.values["total"]) == (3, 4)
    assert s.values["rollout_excluded"] == []
    assert s.values["failed_kinds"] == {"ReplicaSet": "403 Forbidden"}


def test_s02_detects_not_ready_pod_of_other_deployment_not_in_rollout(obs):
    # 한 Service 뒤에 Deployment 2개. agent만 롤아웃 중이고
    # agent-canary(revision 하나뿐)의 Pod는 Ready가 아님
    objects, _ = raw_cluster("s02/rollout")
    objects["ReplicaSet"].append(
        {
            "metadata": {
                "name": "agent-canary-6d5c4b3a2",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"kind": "Deployment", "name": "agent-canary"}],
            },
            "spec": {"replicas": 1},
            "status": {"replicas": 1},
        }
    )
    objects["Pod"].append(
        {
            "metadata": {
                "name": "agent-canary-6d5c4b3a2-c1n2r",
                "labels": {"app": "agent"},
                "ownerReferences": [{"kind": "ReplicaSet", "name": "agent-canary-6d5c4b3a2"}],
            },
            "spec": {"nodeName": "minikube"},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        }
    )
    _by_name(objects, "EndpointSlice", "agent-m4q9t")["endpoints"].append(
        {
            "addresses": ["10.244.0.15"],
            "conditions": {"ready": False, "serving": False, "terminating": False},
            "targetRef": {"kind": "Pod", "name": "agent-canary-6d5c4b3a2-c1n2r"},
        }
    )
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(_snapshot_of(objects), observations=obs)), AGENT_SVC)
    assert (s.verdict, s.values["ready"], s.values["total"]) == ("detected", 3, 4)
    assert s.values["rollout_excluded"] == [NEW_REVISION_POD]
    assert [p["pod"] for p in s.values["not_ready"]] == ["agent-canary-6d5c4b3a2-c1n2r"]


@pytest.mark.parametrize("revision", [None, "abc"])
def test_s02_skips_rollout_exclusion_when_revision_is_unknown(obs, revision):
    # 새 버전을 정할 수 없으면 그 Deployment에는 제외를 적용하지 않고 판정을 계속한다
    objects, _ = raw_cluster("s02/rollout")
    rs = _by_name(objects, "ReplicaSet", "agent-7c9f5d6b4")  # 이전 버전
    rs["metadata"]["annotations"] = (
        {} if revision is None else {"deployment.kubernetes.io/revision": revision}
    )
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=15))
    s = one(run("S02", context(_snapshot_of(objects), observations=obs)), AGENT_SVC)
    assert (s.verdict, s.values["ready"], s.values["total"]) == ("detected", 3, 4)
    assert s.values["rollout_excluded"] == []


def test_s02_recovery_clears_record(obs):
    obs.seed("S02", AGENT_SVC, NOW - timedelta(minutes=30))
    run("S02", context(snapshot(), observations=obs))
    assert obs.get("S02", AGENT_SVC) is None


# ── S03 targetPort 불일치 ──


def test_s03_passes_with_named_and_numeric_ports():
    signals = run("S03", context(snapshot()))
    assert one(signals, AGENT_SVC).verdict == "passed"  # targetPort: http (이름)
    assert one(signals, GW_SVC).verdict == "passed"  # targetPort: 8000 (숫자)


def test_s03_detects_numeric_mismatch():
    s = one(run("S03", context(snapshot("s03/detected"))), AGENT_SVC)
    assert (s.verdict, s.severity) == ("detected", "critical")
    assert s.values["ports"] == [{"port": 8006, "targetPort": 8080, "matched": False}]
    assert s.values["container_ports"] == [{"name": "http", "containerPort": 8006}]


def test_s03_detects_named_mismatch():
    s = one(run("S03", context(snapshot("s03/named"))), AGENT_SVC)
    assert s.verdict == "detected"


def test_s03_without_container_ports_is_unknown():
    s = one(run("S03", context(snapshot("s03/no_container_ports"))), GW_SVC)
    assert s.verdict == "unknown"
    assert s.severity is None


def test_s03_excludes_service_without_pods():
    s = one(run("S03", context(snapshot("s01/detected"))), AGENT_SVC)
    assert s.verdict == "passed"
    assert s.values["excluded"] == "대상 Pod 0개 (S01)"


# ── S04 서비스 주소 설정 오류 ──


def test_s04_passes_declared_address():
    s = one(run("S04", context(snapshot())), GW_SVC)
    assert s.verdict == "passed"
    assert s.values["addresses"] == [
        {
            "env": "AGENT_SERVICE_URL",
            "value": "http://agent:8006",
            "target": "agent",
            "port": 8006,
            "status": "declared",
        }
    ]


def test_s04_detects_localhost_as_critical_on_core_path():
    s = one(run("S04", context(snapshot("s04/localhost"))), GW_SVC)
    assert (s.verdict, s.severity) == ("detected", "critical")
    assert s.values["addresses"][0]["status"] == "unresolved"
    assert s.values["addresses"][0]["value"] == "http://localhost:8006"


def test_s04_sidecar_localhost_passes():
    s = one(run("S04", context(snapshot("s04/sidecar"))), GW_SVC)
    assert s.verdict == "passed"
    assert s.values["addresses"][0]["status"] == "declared"
    # 사이드카 주소임을 근거에 남긴다 (대상이 서비스 자신이라 값이 빠지지 않게)
    assert s.values["addresses"][0]["via"] == "sidecar"
    assert s.values["addresses"][0]["value"].startswith("http://localhost:")


def test_s04_localhost_to_own_container_port_is_still_detected():
    s = one(run("S04", context(snapshot("s04/localhost_own_port"))), GW_SVC)
    assert s.verdict == "detected"


def test_s04_detects_missing_service_and_port():
    s = one(run("S04", context(snapshot("s04/unresolved"))), GW_SVC)
    assert s.verdict == "detected"
    assert {a["env"] for a in s.values["addresses"] if a["status"] == "unresolved"} == {
        "AGENT_SERVICE_URL",
        "ADMIN_SERVICE_URL",
    }


def test_s04_excludes_external_hosts():
    s = one(run("S04", context(snapshot("s04/external"))), GW_SVC)
    assert s.verdict == "passed"
    assert [a["env"] for a in s.values["addresses"]] == ["AGENT_SERVICE_URL"]


def test_s04_keeps_value_per_calling_service():
    # 리뷰 지적: 같은 env 이름·값을 여러 서비스가 쓰면 값이 근거에서 빠졌다
    signals = run("S04", context(snapshot("s04/two_callers")))
    for subj in (GW_SVC, "dev/Deployment/front-chat"):  # front-chat은 선택하는 Service 없음
        s = one(signals, subj)
        assert s.verdict == "detected"
        assert s.values["addresses"][0]["value"] == "http://localhost:8006"


def _topology(monkeypatch, topology):
    import service.checks.detectors.s04 as s04

    monkeypatch.setattr(s04, "build_topology", lambda snapshot, config: topology)


def test_s04_unchecked_env_from_is_unknown(monkeypatch):
    # #37: envFrom ConfigMap을 수집하지 못한 서비스는 확인하지 못함
    topo = Topology(services=["gateway"], edges=[])
    object.__setattr__(topo, "unchecked", {"gateway": ["app-config"]})
    _topology(monkeypatch, topo)
    s = one(run("S04", context(snapshot())), GW_SVC)
    assert s.verdict == "unknown"
    assert s.values["unchecked"] == ["app-config"]


def test_s04_detected_address_wins_over_unchecked(monkeypatch):
    edge = TopologyEdge(
        source="gateway", target="localhost", port=8006, env_name="X_URL", status="unresolved"
    )
    topo = Topology(services=["gateway"], edges=[edge])
    object.__setattr__(topo, "unchecked", {"gateway": ["app-config"]})
    _topology(monkeypatch, topo)
    s = one(run("S04", context(snapshot())), GW_SVC)
    assert s.verdict == "detected"


def test_s04_works_without_unchecked_field(monkeypatch):
    _topology(monkeypatch, Topology(services=["gateway"], edges=[]))
    assert run("S04", context(snapshot())) == []


def _mutated_snapshot(mutate):
    from service.sensors.collector import collect
    from tests.service.checks.detectors.cases import FixtureReader, raw_cluster

    objects, failed = raw_cluster()
    mutate(objects)
    return collect(FixtureReader(objects, failed), now=NOW)


def _snapshot_of(objects, failed=None):
    return collect(FixtureReader(objects, failed or {}), now=NOW)


def _by_name(objects, kind, name):
    return next(o for o in objects[kind] if o["metadata"]["name"] == name)


def test_s02_treats_missing_ready_as_ready():
    # EndpointSlice API: conditions.ready가 nil이면 true로 해석한다
    def mutate(objects):
        for ep in _by_name(objects, "EndpointSlice", "agent-m4q9t")["endpoints"]:
            ep["conditions"] = {}

    s = one(run("S02", context(_mutated_snapshot(mutate))), AGENT_SVC)
    assert (s.verdict, s.values["ready"]) == ("passed", 2)


def test_s01_lists_closest_pod_labels_first():
    # agent Pod는 selector 쌍 하나(app)가 맞고 gateway Pod는 0개다. Pod 목록 순서는 gateway가 먼저
    def mutate(objects):
        _by_name(objects, "Service", "agent")["spec"]["selector"] = {"app": "agent", "tier": "api"}

    s = one(run("S01", context(_mutated_snapshot(mutate))), AGENT_SVC)
    assert s.verdict == "detected"
    assert s.values["similar_pod_labels"][0]["app"] == "agent"
