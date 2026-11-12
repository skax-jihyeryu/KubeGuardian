from datetime import UTC, datetime, timedelta

import pytest

from core.config import ApiScenarioStep, KubeGuardianConfig
from service.config_baseline import observe_baselines, report_baselines
from service.model import ClusterSnapshot, Signal, Symptom

NOW = datetime(2026, 11, 5, tzinfo=UTC)
SENTINEL = "private-config-value-do-not-export"


def config(pin=7):
    return KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "demo"},
            "core_paths": {"chat": ["agent"]},
            "required_config": {"agent": ["LLM_URL"]},
            "api_scenarios": [
                {
                    "name": "chat",
                    "feature": "chat",
                    "steps": [{"call": {"via": "llm-gateway", "path": "/health", "method": "GET"}}],
                }
            ],
            "config_baselines": {
                "agent": {
                    "deployment": "agent",
                    "container": "app",
                    "env": "LLM_URL",
                    "config_map": "shared",
                    "key": "GATEWAY_URL",
                    "expected_service": "llm-gateway",
                    "run_id": pin,
                }
            },
        }
    )


def obj(name, **fields):
    return {"metadata": {"name": name}, **fields}


def snapshot():
    template = {
        "metadata": {"labels": {"app": "agent"}},
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "env": [
                        {
                            "name": "LLM_URL",
                            "valueFrom": {
                                "configMapKeyRef": {"name": "shared", "key": "GATEWAY_URL"}
                            },
                        },
                        {"name": "TOKEN", "value": SENTINEL},
                    ],
                }
            ]
        },
    }
    return ClusterSnapshot(
        namespace="demo",
        collected_at=NOW,
        resources={
            "Deployment": [obj("agent", spec={"template": template}), obj("llm-gateway", spec={})],
            "Service": [
                obj("agent", spec={"selector": {"app": "agent"}}),
                obj("llm-gateway", spec={"ports": [{"port": 8080}]}),
                obj("other", spec={"ports": [{"port": 8080}]}),
            ],
            "ConfigMap": [
                obj(
                    "shared",
                    data={"GATEWAY_URL": f"http://llm-gateway:8080/{SENTINEL}", "TOKEN": SENTINEL},
                )
            ],
            "Secret": [obj("secret", keys=["TOKEN"], data={"TOKEN": SENTINEL})],
            "ReplicaSet": [
                {
                    "metadata": {
                        "name": "agent-replica",
                        "ownerReferences": [{"kind": "Deployment", "name": "agent"}],
                    },
                    "spec": {},
                }
            ],
            "Pod": [
                {
                    "metadata": {
                        "name": "agent-pod",
                        "labels": {"app": "agent"},
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "agent-replica"}],
                    },
                    "spec": template["spec"],
                    "status": {},
                }
            ],
        },
    )


def qualify(cfg, snap, status="passed"):
    signals = [
        Signal(rule_id="C01", verdict="passed", subject="demo/Pod/agent-pod", service="agent"),
        Signal(rule_id="C05", verdict="passed", subject="demo/Service/agent", service="agent"),
    ]
    symptoms = [Symptom(scenario="chat", status=status, summary=SENTINEL)]
    observe_baselines(cfg, snap, signals, symptoms)


def report(cfg, current, pinned, *, done=True, now=NOW):
    return report_baselines(
        cfg,
        current,
        now=now,
        run_id=8,
        load_pinned=lambda run_id: (pinned, done) if run_id == 7 else (None, False),
    )[0]


def test_shared_config_map_expected_relation_and_pinned_normal_observation():
    cfg = config()
    previous = snapshot()
    qualify(cfg, previous)
    current = snapshot()
    current.resources["ConfigMap"][0]["data"]["GATEWAY_URL"] = f"http://other:8080/{SENTINEL}"
    result = report(cfg, current, previous)

    assert result.expected.result == "mismatch"
    assert result.expected.observed_service == "other"
    assert result.baseline.result == "approved"
    assert result.baseline.run_id == 7
    assert result.baseline.collected_at == NOW
    assert SENTINEL not in result.model_dump_json()
    assert SENTINEL not in previous.config_baseline.model_dump_json()


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ("legacy", "legacy"),
        ("project", "target_mismatch"),
        ("namespace", "target_mismatch"),
        ("intent", "intent_mismatch"),
        ("incomplete", "check_incomplete"),
        ("failed_scenario", "observations_not_passed"),
        ("missing_kind", "collection_incomplete"),
        ("stale", "stale"),
        ("missing_run", "snapshot_missing"),
        ("unselected", "not_pinned"),
    ],
)
def test_baseline_rejects_unqualified_snapshots(variant, reason):
    cfg = config(None if variant == "unselected" else 7)
    previous = snapshot()
    if variant != "legacy":
        qualify(cfg, previous, "unknown" if variant == "failed_scenario" else "passed")
    if variant == "project":
        cfg.project.id = "different"
    elif variant == "namespace":
        previous.namespace = "different"
    elif variant == "intent":
        cfg.config_baselines["agent"].expected_service = "other"
    elif variant == "missing_kind":
        previous.failed["ConfigMap"] = SENTINEL
    elif variant == "missing_run":
        previous = None
    now = NOW + timedelta(days=60) if variant == "stale" else NOW
    result = report(cfg, snapshot(), previous, done=variant != "incomplete", now=now)
    assert result.baseline.result == "unavailable"
    assert reason in result.baseline.limitations
    assert SENTINEL not in result.model_dump_json()


@pytest.mark.parametrize(
    ("data", "verdict"), [({}, "missing"), ({"GATEWAY_URL": SENTINEL}, "unsupported")]
)
def test_missing_or_unsupported_setting(data, verdict):
    current = snapshot()
    current.resources["ConfigMap"][0]["data"] = data
    result = report(config(None), current, None)
    assert result.expected.result == verdict
    assert SENTINEL not in result.model_dump_json()


def test_check_persists_baseline_provenance_and_only_uses_explicit_run(tmp_path):
    from core.db import CheckRunStore, create_db_engine
    from service.pipeline.check import run_check
    from tests.service.checks.detectors.cases import FixtureReader
    from tests.service.tools.fakes import FakeHttp

    engine = create_db_engine(tmp_path / "baseline.db")
    cfg = config(None)
    cfg.target.namespace = "dev"
    cfg.api_scenarios[0].steps = [
        ApiScenarioStep.model_validate(
            {"call": {"via": "llm-gateway", "path": "/health", "method": "GET"}}
        )
    ]
    resources = snapshot().resources
    reader = FixtureReader(resources, {})
    http = FakeHttp({("llm-gateway", "/health"): 200})
    first = run_check(reader, cfg, engine, trigger="check", now=NOW, http=http)
    assert first.config_baselines[0].baseline.limitations == ("not_pinned",)
    assert CheckRunStore(engine).get_snapshot(first.run_id).config_baseline is not None

    cfg.config_baselines["agent"].run_id = first.run_id
    latest = run_check(reader, cfg, engine, trigger="check", now=NOW, http=http)
    assert latest.config_baselines[0].baseline.result == "approved"
    assert latest.config_baselines[0].baseline.run_id == first.run_id

    third = run_check(reader, cfg, engine, trigger="check", now=NOW, http=http)
    assert third.config_baselines[0].baseline.run_id == first.run_id


@pytest.mark.parametrize(
    "missing", ["pod_verdict", "required_verdict", "scenario", "scenario_config"]
)
def test_missing_observation_does_not_count_as_pass(missing):
    cfg = config()
    previous = snapshot()
    signals = [
        Signal(rule_id="C01", verdict="passed", subject="demo/Pod/agent-pod", service="agent"),
        Signal(rule_id="C05", verdict="passed", subject="demo/Service/agent", service="agent"),
    ]
    if missing == "pod_verdict":
        signals.pop(0)
    if missing == "required_verdict":
        signals.pop()
    if missing == "scenario_config":
        cfg.api_scenarios = []
    symptoms = (
        [] if missing == "scenario" else [Symptom(scenario="chat", status="passed", summary="")]
    )
    observe_baselines(cfg, previous, signals, symptoms)
    result = report(cfg, snapshot(), previous)
    assert result.baseline.limitations == ("observations_not_passed",)


def test_env_from_prefix_and_secret_shadowing():
    cfg = config(None)
    cfg.config_baselines["agent"].env = "APP_GATEWAY_URL"
    current = snapshot()
    container = current.resources["Deployment"][0]["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"prefix": "APP_", "configMapRef": {"name": "shared"}}]
    assert report(cfg, current, None).expected.result == "matched"

    current.resources["Secret"][0]["keys"].append("GATEWAY_URL")
    container["envFrom"].append({"prefix": "APP_", "secretRef": {"name": "secret"}})
    assert report(cfg, current, None).expected.result == "unsupported"


def test_old_config_omits_provenance_and_does_not_read_a_pin():
    cfg = KubeGuardianConfig.model_validate({"target": {"namespace": "demo"}})
    current = snapshot()
    observe_baselines(cfg, current, [], [])

    def unexpected_load(run_id):
        pytest.fail(f"unexpected pin {run_id}")

    assert report_baselines(cfg, current, now=NOW, run_id=8, load_pinned=unexpected_load) == []
    assert "config_baseline" not in current.model_dump()


def test_empty_scenario_does_not_qualify_a_baseline():
    cfg = config()
    cfg.api_scenarios[0].steps = []
    previous = snapshot()
    qualify(cfg, previous)
    assert report(cfg, snapshot(), previous).baseline.result == "unavailable"


def test_pod_without_declared_binding_does_not_qualify_a_baseline():
    cfg = config()
    previous = snapshot()
    previous.resources["Pod"][0]["spec"] = {"containers": [{"name": "app"}]}
    qualify(cfg, previous)

    result = report(cfg, previous, previous)
    assert result.expected.result == "matched"
    assert result.baseline.result == "unavailable"


@pytest.mark.parametrize("kind", ["configMapKeyRef", "secretKeyRef"])
def test_absent_optional_env_key_does_not_shadow_env_from(kind):
    cfg = config(None)
    current = snapshot()
    container = current.resources["Deployment"][0]["spec"]["template"]["spec"]["containers"][0]
    cfg.config_baselines["agent"].env = "GATEWAY_URL"
    container["env"] = [
        {
            "name": "GATEWAY_URL",
            "valueFrom": {kind: {"name": "missing", "key": "URL", "optional": True}},
        },
    ]
    container["envFrom"] = [{"configMapRef": {"name": "shared"}}]

    assert report(cfg, current, None).expected.result == "matched"
