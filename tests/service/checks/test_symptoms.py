import pytest

from core.config import KubeGuardianConfig
from core.guard.budget import Budget, BudgetTracker
from core.http import HttpResponse, NamespacedHttp
from core.k8s import K8sCallError
from service.checks.symptoms import detect_symptoms
from service.tools.base import ToolContext
from tests.service.tools.fakes import NOW, FakeHttp, FakeK8s, MemoryEvidence, service


@pytest.fixture
def evidence():
    return MemoryEvidence()


@pytest.fixture
def make_ctx(evidence):
    def make(k8s, http):
        return ToolContext(
            namespace="dev",
            evidence=evidence,
            budget=BudgetTracker(Budget()),
            k8s=k8s,
            now=lambda: NOW,
            http=http,
        )

    return make


def _scenario(name, feature="chat"):
    return {
        "name": name,
        "feature": feature,
        "steps": [
            {"call": {"via": "gateway", "path": f"/{name}", "method": "GET", "expect": 200}},
        ],
    }


def _config(*names, features=None):
    """features: 시나리오 이름 → 기능 (기본 chat). core_paths는 쓰인 기능을 모두 등록한다."""
    features = features or {}
    scenarios = [_scenario(n, features.get(n, "chat")) for n in names]
    core_paths = {s["feature"]: ["gateway"] for s in scenarios}
    return KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "core_paths": core_paths,
            "api_scenarios": scenarios,
        }
    )


def _k8s():
    return FakeK8s({"Service": [service("gateway", port=80)]})


def _ctx(make_ctx, http, k8s=None):
    return make_ctx(k8s or _k8s(), http)


def test_passing_scenario_is_not_a_symptom(make_ctx):
    http = FakeHttp({("gateway", "/chat"): HttpResponse(200, 10.0, "{}")})

    [s] = detect_symptoms(_config("chat"), _ctx(make_ctx, http))

    assert (s.scenario, s.failed_step, s.severity) == ("chat", None, None)
    assert "모두 성공" in s.summary
    assert s.evidence_id == "ev-0001"


def test_failed_scenario_severity_follows_its_feature_not_its_name(make_ctx, evidence):
    http = FakeHttp({("gateway", "/chat-flow"): 500})

    [s] = detect_symptoms(
        _config("chat-flow", features={"chat-flow": "chat"}), _ctx(make_ctx, http)
    )

    assert (s.scenario, s.failed_step, s.severity) == ("chat-flow", 1, "critical")
    assert "1단계 실패" in s.summary
    assert evidence.saved[0].subject == "dev/Scenario/chat-flow"


def test_runs_every_scenario_in_config_order(make_ctx):
    http = FakeHttp({("gateway", "/a"): 500, ("gateway", "/b"): HttpResponse(200, 1.0, "{}")})

    out = detect_symptoms(_config("a", "b"), _ctx(make_ctx, http))

    assert [(s.scenario, s.failed_step) for s in out] == [("a", 1), ("b", None)]


def test_missing_target_service_is_a_failed_step_not_unknown(make_ctx):
    # Service 목록은 읽었는데 연결할 Service가 없으면 사용자는 실패한다 (05-rules §3)
    http = FakeHttp()

    [s] = detect_symptoms(_config("chat"), _ctx(make_ctx, http, FakeK8s({"Service": []})))

    assert (s.status, s.failed_step, s.severity) == ("failed", 1, "critical")
    assert "Service gateway" in s.summary
    assert http.calls == []


def test_service_without_ports_is_a_failed_step(make_ctx):
    no_port = service("gateway")
    no_port["spec"]["ports"] = []

    [s] = detect_symptoms(
        _config("chat"), _ctx(make_ctx, FakeHttp(), FakeK8s({"Service": [no_port]}))
    )

    assert (s.status, s.failed_step) == ("failed", 1)
    assert "포트" in s.summary


def test_service_list_collection_failure_is_unknown(make_ctx):
    # 목록을 읽지 못했으면 Service가 없는지 모른다
    k8s = FakeK8s({"Service": K8sCallError("api down")})

    [s] = detect_symptoms(_config("chat"), _ctx(make_ctx, FakeHttp(), k8s))

    assert (s.status, s.failed_step, s.severity) == ("unknown", None, None)
    assert "Service" in s.summary


def test_external_name_service_is_unknown(make_ctx):
    external = service("gateway")
    external["spec"] = {"type": "ExternalName", "externalName": "x.other-ns.svc.cluster.local"}

    [s] = detect_symptoms(
        _config("chat"), _ctx(make_ctx, FakeHttp(), FakeK8s({"Service": [external]}))
    )

    assert s.status == "unknown"
    assert "ExternalName" in s.summary


def test_missing_test_account_is_unknown_without_values(make_ctx):
    config = KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "core_paths": {"chat": ["gateway"]},
            "api_scenarios": [
                {
                    "name": "chat",
                    "feature": "chat",
                    "steps": [
                        {
                            "login": {
                                "via": "gateway",
                                "path": "/login",
                                "account_secret": "kg-test-user",
                            }
                        }
                    ],
                }
            ],
        }
    )

    [s] = detect_symptoms(config, _ctx(make_ctx, NamespacedHttp("dev")))

    assert s.status == "unknown"
    assert "테스트 계정 Secret kg-test-user 없음" in s.summary


def test_statuses_of_passed_and_failed_scenarios(make_ctx):
    http = FakeHttp({("gateway", "/a"): 500, ("gateway", "/b"): HttpResponse(200, 1.0, "{}")})

    out = detect_symptoms(_config("a", "b"), _ctx(make_ctx, http))

    assert [s.status for s in out] == ["failed", "passed"]


def test_no_scenarios_means_no_symptoms(make_ctx):
    assert detect_symptoms(_config(), _ctx(make_ctx, FakeHttp())) == []
