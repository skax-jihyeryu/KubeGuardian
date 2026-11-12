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
    from core.db import CheckRunStore, EvidenceStore, create_db_engine
    from service.config_change import CONFIG_CHANGE_SOURCE
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
    previous, _ = _comparison_snapshots(cfg)
    reader = FixtureReader(previous.resources, {})
    http = FakeHttp({("llm-gateway", "/health"): 200})
    first = run_check(reader, cfg, engine, trigger="check", now=NOW, http=http)
    assert first.config_baselines[0].baseline.limitations == ("not_pinned",)
    assert CheckRunStore(engine).get_snapshot(first.run_id).config_baseline is not None

    cfg.config_baselines["agent"].run_id = first.run_id
    latest = run_check(reader, cfg, engine, trigger="check", now=NOW, http=http)
    assert latest.config_baselines[0].baseline.result == "approved"
    assert latest.config_baselines[0].baseline.run_id == first.run_id
    stored = EvidenceStore(engine).list_for_run(latest.run_id, source=CONFIG_CHANGE_SOURCE)
    assert len(stored) == 1
    assert stored[0].data["status"] == "no_change"
    assert stored[0].data["symptoms"][0]["status"] == "passed"
    assert SENTINEL not in stored[0].model_dump_json()

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


def _comparison_snapshots(cfg):
    previous = snapshot()
    deployment = previous.resources["Deployment"][0]["metadata"]
    deployment["uid"] = "deployment-uid"
    rs = previous.resources["ReplicaSet"][0]["metadata"]
    rs.update(uid="replica-uid")
    rs["ownerReferences"][0].update(uid=deployment["uid"], controller=True)
    pod = previous.resources["Pod"][0]["metadata"]
    pod.update(uid="pod-uid", creationTimestamp=(NOW - timedelta(hours=1)).isoformat())
    pod["ownerReferences"][0].update(uid=rs["uid"], controller=True)
    qualify(cfg, previous)
    current = previous.model_copy(deep=True)
    current.collected_at = NOW + timedelta(minutes=10)
    return previous, current


@pytest.mark.parametrize(
    ("variant", "status", "key_presence", "key_value", "binding", "pods"),
    [
        ("same", "no_change", "no_change", "no_change", "no_change", "no_change"),
        ("value", "changed", "no_change", "changed", "no_change", "no_change"),
        ("deleted", "changed", "changed", "unknown", "no_change", "no_change"),
        ("replaced", "changed", "no_change", "no_change", "no_change", "changed"),
        ("binding", "changed", "no_change", "no_change", "changed", "no_change"),
        ("masked", "unknown", "no_change", "unknown", "no_change", "no_change"),
        ("failed", "unknown", "unknown", "unknown", "no_change", "no_change"),
        ("unqualified", "unknown", "unknown", "unknown", "unknown", "unknown"),
        ("unfinished", "unknown", "unknown", "unknown", "unknown", "unknown"),
        ("other_intent", "unknown", "unknown", "unknown", "unknown", "unknown"),
    ],
)
def test_compare_config_change(variant, status, key_presence, key_value, binding, pods):
    from service.config_change import compare_config_change

    cfg = config()
    previous, current = _comparison_snapshots(cfg)
    data = current.resources["ConfigMap"][0]["data"]
    if variant == "value":
        data["GATEWAY_URL"] += "-changed"
    elif variant == "deleted":
        data.pop("GATEWAY_URL")
    elif variant == "replaced":
        current.resources["Pod"][0]["metadata"].update(
            uid="replacement-uid", creationTimestamp=(NOW + timedelta(minutes=1)).isoformat()
        )
    elif variant == "binding":
        current.resources["Deployment"][0]["spec"]["template"]["spec"]["containers"][0]["env"][0][
            "valueFrom"
        ]["configMapKeyRef"]["key"] = "OTHER_URL"
    elif variant == "masked":
        data["GATEWAY_URL"] = "http://user:***@llm-gateway:8080"
    elif variant == "failed":
        current.failed["ConfigMap"] = SENTINEL
    elif variant == "unqualified":
        previous.config_baseline = None
    elif variant == "other_intent":
        qualify(config(pin=7).model_copy(update={"external_hosts": ["different.example"]}), current)
    result = compare_config_change(
        cfg,
        "agent",
        current_run_id=8,
        load_check=lambda rid: (
            previous if rid == 7 else current,
            rid == 7 or variant != "unfinished",
        ),
    )
    assert result["status"] == status
    assert result["key_presence"]["status"] == key_presence
    assert result["key_value"] == key_value
    assert result["deployment_env"] == binding
    assert result["owned_pods"]["status"] == pods
    assert result["env_application"] == "unverified"
    assert result["baseline"]["run_id"] == 7 and result["current"]["run_id"] == 8
    assert ("interval" in result) == (variant not in {"unqualified", "unfinished", "other_intent"})
    assert SENTINEL not in str(result)
    assert "http://" not in str(result)
    if variant == "replaced":
        assert result["owned_pods"]["created"][0]["uid"] == "replacement-uid"
        assert result["owned_pods"]["removed"][0]["uid"] == "pod-uid"


def test_shared_key_deletion_preserves_consumer_references():
    from service.config_change import compare_config_change

    cfg = config()
    baseline, current = _comparison_snapshots(cfg)
    for snap in (baseline, current):
        snap.resources["Deployment"].append(
            obj(
                "another-agent",
                spec={
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "worker",
                                    "envFrom": [
                                        {"prefix": "APP_", "configMapRef": {"name": "shared"}}
                                    ],
                                }
                            ]
                        }
                    }
                },
            )
        )
    del current.resources["ConfigMap"][0]["data"]["GATEWAY_URL"]
    result = compare_config_change(
        cfg,
        "agent",
        current_run_id=8,
        load_check=lambda rid: (baseline if rid == 7 else current, True),
    )
    assert result["declared_consumers"]["current"] == [
        {"deployment": "agent", "container": "app", "env": "LLM_URL", "source": "env"},
        {
            "deployment": "another-agent",
            "container": "worker",
            "env": "APP_GATEWAY_URL",
            "source": "envFrom",
        },
    ]


def test_config_change_evidence_is_persisted_and_linkable(tmp_path):
    from core.db import CheckResultStore, CheckRunStore, EvidenceStore, create_db_engine
    from service.config_change import CONFIG_CHANGE_SOURCE
    from service.model import Evidence
    from service.pipeline.config_change import investigation_comparison, record_config_changes
    from service.tools import build_tools
    from service.tools.base import ToolContext
    from tests.service.tools.fakes import FakeK8s
    from web.viewmodels.checks import build_drawer

    engine = create_db_engine(tmp_path / "comparison.db")
    runs, results = CheckRunStore(engine), CheckResultStore(engine)
    baseline_id, current_id = runs.create("check", NOW), runs.create("check", NOW)
    cfg = config(baseline_id)
    baseline, current = _comparison_snapshots(cfg)
    del current.resources["ConfigMap"][0]["data"]["GATEWAY_URL"]
    for rid, snap in [(baseline_id, baseline), (current_id, current)]:
        results.start(rid, snap.collected_at)
        runs.save_snapshot(rid, snap)
    sink = EvidenceStore(engine, current_id)
    fail_time = current.collected_at + timedelta(seconds=3)
    failed = sink.add(
        Evidence(
            kind="app_api",
            subject="demo/Scenario/chat",
            source="scenario chat",
            collected_at=fail_time,
            data={"scenario": "chat", "failed_step": 1, "body": SENTINEL},
        )
    )
    other = sink.add(
        Evidence(
            kind="app_api",
            subject="other/Scenario/chat",
            source="scenario chat",
            collected_at=fail_time,
            data={"scenario": "chat", "failed_step": 1, "body": SENTINEL},
        )
    )
    finish_time = fail_time + timedelta(seconds=1)
    results.finish(baseline_id, "done", NOW, None, [], [])
    results.finish(
        current_id,
        "done",
        finish_time,
        None,
        [],
        [
            Symptom(
                scenario="chat",
                status="failed",
                summary="failed",
                failed_step=1,
                evidence_id=failed,
            ),
            Symptom(
                scenario="chat", status="failed", summary="failed", failed_step=1, evidence_id=other
            ),
        ],
    )

    record_config_changes(engine, cfg, current_id, finish_time)
    record_config_changes(engine, cfg, current_id, finish_time)
    records = [ev for ev in sink.list_for_run(current_id) if ev.source == CONFIG_CHANGE_SOURCE]
    assert len(records) == 1
    record = records[0]
    assert record.data["status"] == "changed"
    assert record.data["symptoms"][0]["evidence_id"] == failed
    assert len(record.data["symptoms"]) == 1
    assert SENTINEL not in record.model_dump_json()
    drawer = build_drawer(engine, current_id, int(record.id[3:]), finish_time)
    assert drawer is not None
    assert drawer.related_evidence == [int(failed[3:])]
    assert build_drawer(engine, baseline_id, int(record.id[3:]), finish_time) is None

    tool = next(
        t
        for t in build_tools(
            cfg, config_comparison=investigation_comparison(engine, cfg, current_id)
        )
        if t.name == "get_resource"
    )
    first = (current.collected_at - timedelta(seconds=2)).isoformat()
    observed_at = finish_time + timedelta(seconds=30)
    k8s = FakeK8s(
        current.resources,
        logs={("agent-pod", "app", False): f"{first} ERROR {SENTINEL}"},
    )
    k8s.namespace = "demo"
    log_tool = next(
        t
        for t in build_tools(
            cfg, config_comparison=investigation_comparison(engine, cfg, current_id)
        )
        if t.name == "get_pod_logs"
    )
    log = log_tool.run(
        {"pod": "agent-pod", "container": "app"},
        ToolContext("demo", sink, k8s=k8s, now=lambda: observed_at),
    )
    assert log.ok
    assert log.data["pod_uid"] == "pod-uid"
    for lines in (
        f"embedded application timestamp {first} ERROR {SENTINEL}",
        f"{first} ERROR\n{(NOW - timedelta(minutes=1)).isoformat()} ERROR\n{first} ERROR",
    ):
        k8s.logs[("agent-pod", "app", False)] = lines
        assert log_tool.run(
            {"pod": "agent-pod", "container": "app"},
            ToolContext("demo", sink, k8s=k8s, now=lambda: observed_at),
        ).ok
    sink.add(
        Evidence(
            kind="log_excerpt",
            subject="demo/Pod/agent-pod",
            source="GET log",
            collected_at=observed_at,
            data={**log.data, "pod_uid": "unrelated-uid"},
        )
    )
    outcome = tool.run(
        {"kind": "Deployment", "name": "agent"},
        ToolContext("demo", sink, k8s=k8s, now=lambda: observed_at),
    )
    changes = outcome.data["config_changes"]
    assert changes[0]["evidence_id"] == record.id
    assert changes[0]["key_presence"]["current"] == "missing"
    assert changes[0]["logs"] == [
        {
            "pod": "agent-pod",
            "first_event_at": first,
            "last_event_at": first,
            "observed_at": observed_at.isoformat(),
            "evidence_id": log.evidence_id,
        }
    ]
    assert SENTINEL not in str(changes)
    drawer = build_drawer(engine, current_id, int(outcome.evidence_id[3:]), observed_at)
    assert drawer.related_evidence == [int(record.id[3:]), int(log.evidence_id[3:])]

    for changed in (
        cfg.model_copy(update={"external_hosts": ["different.example"]}),
        config(pin=None),
        cfg.model_copy(update={"project": cfg.project.model_copy(update={"id": "another"})}),
    ):
        comparison = investigation_comparison(engine, changed, current_id)("agent")
        assert comparison["status"] == "unknown"
        assert "evidence_id" not in comparison


def test_scoped_config_change_logs_reject_a_foreign_namespace(tmp_path):
    from core.db import EvidenceStore, create_db_engine
    from core.error.service_exception import ErrorCode, ServiceException
    from service.tools.base import ToolContext
    from service.tools.logs import GetPodLogs
    from tests.service.tools.fakes import FakeK8s

    engine = create_db_engine(tmp_path / "logs.db")
    k8s = FakeK8s(logs={("agent-pod", "app", False): "foreign log"})
    ctx = ToolContext("demo", EvidenceStore(engine), k8s=k8s)
    with pytest.raises(ServiceException) as rejected:
        GetPodLogs(verify_pod_identity=True).run({"pod": "agent-pod", "container": "app"}, ctx)
    assert rejected.value.error_code is ErrorCode.TOOL_SCOPE_VIOLATION
    assert not k8s.log_calls
