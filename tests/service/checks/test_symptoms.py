import pytest

from core.config import KubeGuardianConfig
from core.guard.budget import Budget, BudgetTracker
from core.http import HttpResponse
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


def _scenario(name):
    return {
        "name": name,
        "steps": [
            {"call": {"via": "gateway", "path": f"/{name}", "method": "GET", "expect": 200}},
        ],
    }


def _config(*names, core_paths=None):
    return KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "core_paths": core_paths or {},
            "api_scenarios": [_scenario(n) for n in names],
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


def test_failed_scenario_in_core_paths_is_critical(make_ctx, evidence):
    http = FakeHttp({("gateway", "/chat"): 500})

    [s] = detect_symptoms(_config("chat", core_paths={"chat": ["gateway"]}), _ctx(make_ctx, http))

    assert (s.scenario, s.failed_step, s.severity) == ("chat", 1, "critical")
    assert "1단계 실패" in s.summary
    assert evidence.saved[0].subject == "dev/Scenario/chat"


def test_failed_scenario_outside_core_paths_is_warning(make_ctx):
    http = FakeHttp({("gateway", "/export"): 500})

    [s] = detect_symptoms(_config("export", core_paths={"chat": ["gateway"]}), _ctx(make_ctx, http))

    assert (s.failed_step, s.severity) == (1, "warning")


def test_runs_every_scenario_in_config_order(make_ctx):
    http = FakeHttp({("gateway", "/a"): 500, ("gateway", "/b"): HttpResponse(200, 1.0, "{}")})

    out = detect_symptoms(_config("a", "b"), _ctx(make_ctx, http))

    assert [(s.scenario, s.failed_step) for s in out] == [("a", 1), ("b", None)]


def test_scenario_that_could_not_run_is_not_a_symptom(make_ctx):
    # Service가 없으면 도구가 실행하지 못한다 (ToolResult 오류). 규칙이 따로 잡는다
    http = FakeHttp()

    out = detect_symptoms(_config("chat"), _ctx(make_ctx, http, FakeK8s({"Service": []})))

    assert out == []
    assert http.calls == []


def test_no_scenarios_means_no_symptoms(make_ctx):
    assert detect_symptoms(_config(), _ctx(make_ctx, FakeHttp())) == []
