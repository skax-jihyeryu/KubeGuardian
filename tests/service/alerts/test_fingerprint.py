from core.config import KubeGuardianConfig
from service.alerts.fingerprint import OTHER_FEATURE, compute_fingerprint
from service.checks.gate import decide_gate
from service.model import Signal, Symptom

CONFIG = KubeGuardianConfig.model_validate(
    {
        "target": {"namespace": "dev"},
        "core_paths": {"login": ["gateway", "admin"], "chat": ["gateway", "agent"]},
        "api_scenarios": [
            {
                "name": "chat",
                "feature": "chat",
                "steps": [{"call": {"via": "gateway", "path": "/c"}}],
            }
        ],
    }
)


def _sig(rule="S01", service="agent", subject=None, severity="critical"):
    return Signal(
        rule_id=rule,
        verdict="detected",
        subject=subject or f"dev/Service/{service}",
        service=service,
        severity=severity,
    )


def _failed(scenario="chat", step=2):
    return Symptom(
        scenario=scenario, status="failed", failed_step=step, summary="500", severity="critical"
    )


def _fp(signals=(), symptoms=(), user=None):
    return compute_fingerprint(decide_gate(list(signals), list(symptoms), user), CONFIG)


def test_fingerprint_has_path_rules_steps_and_services():
    fp = _fp([_sig("S01", "agent"), _sig("P03", "agent", "dev/Pod/agent-1/agent")], [_failed()])

    assert fp.path == "A"
    assert fp.rule_ids == ("P03", "S01")
    assert fp.failed_steps == ("chat:2",)
    assert fp.services == ("agent",)
    assert fp.features == ("chat",)


def test_same_inputs_in_other_order_give_same_key():
    a = _fp([_sig("S01", "agent"), _sig("S04", "gateway")])
    b = _fp([_sig("S04", "gateway"), _sig("S01", "agent")])

    assert a.key == b.key


def test_new_signal_or_failed_step_changes_key():
    base = _fp([_sig("S01", "agent")])

    assert _fp([_sig("S01", "agent"), _sig("P03", "agent")]).key != base.key
    assert _fp([_sig("S01", "agent")], [_failed()]).key != base.key
    assert _fp([], [_failed(step=1)]).key != _fp([], [_failed(step=2)]).key


def test_subject_change_within_same_rule_and_service_keeps_key():
    # Pod 이름은 재시작마다 바뀐다. 지문은 규칙·서비스 단위로 본다
    a = _fp([_sig("P03", "agent", "dev/Pod/agent-abc/agent")])
    b = _fp([_sig("P03", "agent", "dev/Pod/agent-xyz/agent")])

    assert a.key == b.key
    assert a.targets != b.targets  # 해결 판정은 실제 대상으로 한다


def test_features_come_from_core_paths_and_scenarios():
    assert _fp([_sig("S04", "gateway")]).features == ("chat", "login")
    assert _fp([], [_failed()]).features == ("chat",)


def test_service_outside_core_paths_is_other_feature():
    assert _fp([_sig("P06", "redis", severity="warning")]).features == (OTHER_FEATURE,)
    assert _fp([], [], user="느려요").features == (OTHER_FEATURE,)


def test_path_b_fingerprint_keeps_failed_scenarios():
    fp = _fp([], [_failed()])

    assert (fp.path, fp.rule_ids, fp.scenarios) == ("B", (), ("chat",))
