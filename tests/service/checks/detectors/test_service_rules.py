"""S01·S02·S03·S04 (05-rules §4, §7)."""

from datetime import timedelta

from service.model import Topology, TopologyEdge
from tests.service.checks.detectors.cases import NOW, context, one, run, snapshot

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
