from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import KubeGuardianConfig, load_kubeguardian_config

EXAMPLE = Path(__file__).resolve().parents[2] / "kubeguardian.example.yaml"


def test_example_config_loads():
    config = load_kubeguardian_config(EXAMPLE)

    assert config.target.namespace == "dev"
    assert "chat" in config.core_paths
    assert config.topology.url_env_patterns == ["*_SERVICE_URL", "*_URL"]


def test_misspelled_key_is_rejected(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text("target:\n  namespace: dev\ncore_path:\n  chat: [gateway]\n")

    with pytest.raises(ValidationError, match="core_path"):
        load_kubeguardian_config(path)


def test_target_namespace_is_required(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text("schedule: '*/5 * * * *'\n")

    with pytest.raises(ValidationError, match="target"):
        load_kubeguardian_config(path)


@pytest.mark.parametrize(
    "snippet",
    [
        "slack: {alert_target: C0123456789, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critcal: {first_after: 30m, max: 3}}}",
        "slack: {alert_target: C0123456789, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critical: {first_after: 30분, max: 3}}}",
        "slack: {alert_target: C0123456789, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critical: {first_after: 30m, max: 0}}}",
        "schedule: nope",
        "snapshot_retention_days: 0",
    ],
)
def test_invalid_values_are_rejected(tmp_path, snippet):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text("target: {namespace: dev}\n" + snippet + "\n")

    with pytest.raises(ValidationError):
        load_kubeguardian_config(path)


@pytest.mark.parametrize("namespace", ["", "Dev_NS"])
def test_namespace_must_be_dns_label(tmp_path, namespace):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(f"target: {{namespace: '{namespace}'}}\n")

    with pytest.raises(ValidationError):
        load_kubeguardian_config(path)


def test_required_config_must_map_service_to_name_list():
    base = {"target": {"namespace": "dev"}}
    ok = KubeGuardianConfig.model_validate({**base, "required_config": {"gateway": ["A"]}})
    assert ok.required_config == {"gateway": ["A"]}
    assert KubeGuardianConfig.model_validate(base).required_config == {}
    with pytest.raises(ValidationError):
        KubeGuardianConfig.model_validate({**base, "required_config": {"gateway": "A"}})


_STEP = {"call": {"via": "gateway", "path": "/x"}}


def _with_scenarios(*scenarios):
    return KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "core_paths": {"chat": ["gateway"]},
            "api_scenarios": list(scenarios),
        }
    )


def test_scenario_with_registered_feature_loads():
    config = _with_scenarios({"name": "chat-flow", "feature": "chat", "steps": [_STEP]})

    assert config.api_scenarios[0].feature == "chat"


@pytest.mark.parametrize(
    "scenario",
    [
        {"name": "chat", "steps": [_STEP]},  # feature 누락
        {"name": "chat", "feature": "export", "steps": [_STEP]},  # core_paths에 없는 키
        {"name": "a b", "feature": "chat", "steps": [_STEP]},  # 도구 입력 패턴 밖 이름
        {"name": "x" * 65, "feature": "chat", "steps": [_STEP]},
    ],
)
def test_invalid_scenario_is_rejected_at_load(scenario):
    with pytest.raises(ValidationError):
        _with_scenarios(scenario)


def test_duplicate_scenario_name_is_rejected():
    scenario = {"name": "chat", "feature": "chat", "steps": [_STEP]}

    with pytest.raises(ValidationError, match="중복"):
        _with_scenarios(scenario, scenario)


def test_excluded_event_groups_default_empty():
    config = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}})

    assert config.exclude_event_api_groups == []


def test_example_config_hides_chaos_mesh_events():
    # 이 PoC는 Chaos Mesh로 장애를 주입한다. 주입 흔적이 조사 입력에 들어가면 안 된다 (#82)
    assert "chaos-mesh.org" in load_kubeguardian_config(EXAMPLE).exclude_event_api_groups


def test_slack_needs_only_alert_target():
    # 10/31: Slack은 알림만. 명령용 앱 토큰·허용 채널은 선택 (예전 설정도 거부하지 않음)
    config = KubeGuardianConfig.model_validate(
        {"target": {"namespace": "dev"}, "slack": {"alert_target": "C0123456789"}}
    )
    assert config.slack.app_token_secret is None and config.slack.bot_token_secret is None

    old = KubeGuardianConfig.model_validate(
        {
            "target": {"namespace": "dev"},
            "slack": {
                "alert_target": "C0123456789",
                "app_token_secret": "kg-slack-app",
                "allowed_users": [],
            },
        }
    )
    assert old.slack.app_token_secret == "kg-slack-app"


@pytest.mark.parametrize("target", ["#ops", "ops", "c0123456789", "C123"])
def test_alert_target_rejects_channel_name(target):
    with pytest.raises(ValidationError, match="채널 ID"):
        KubeGuardianConfig.model_validate(
            {"target": {"namespace": "dev"}, "slack": {"alert_target": target}}
        )


@pytest.mark.parametrize("target", ["C0123456789", "G0123456789", "D0123456789"])
def test_alert_target_accepts_channel_id(target):
    config = KubeGuardianConfig.model_validate(
        {"target": {"namespace": "dev"}, "slack": {"alert_target": target}}
    )
    assert config.slack.alert_target == target


def test_app_api_auth_accepts_own_org_template_and_is_optional():
    login = {
        "via": "gateway",
        "path": "/login",
        "account_secret": "kg-test-user",
        "extract": {"org_id": "user.role.organizations.0.org_id"},
    }
    config = KubeGuardianConfig(
        target={"namespace": "dev"},
        app_apis={"gateway": ["/assignments/org/${org_id}"]},
        app_api_auth={"gateway": login},
    )
    assert config.app_api_auth["gateway"].account_secret == "kg-test-user"
    assert KubeGuardianConfig(target={"namespace": "dev"}).app_api_auth == {}


@pytest.mark.parametrize(
    "apis, auth",
    [
        ({}, {"gateway": {"via": "gateway", "path": "/login", "account_secret": "s"}}),
        ({"gateway": []}, {"gateway": {"via": "gateway", "path": "/login", "account_secret": "s"}}),
        ({"gateway": ["/org/${org_id}"]}, {}),
        (
            {"gateway": ["/org/${org_id}"]},
            {"gateway": {"via": "gateway", "path": "/login", "account_secret": "s"}},
        ),
    ],
)
def test_app_api_auth_requires_allowlist_and_matching_extract(apis, auth):
    with pytest.raises(ValidationError):
        KubeGuardianConfig(target={"namespace": "dev"}, app_apis=apis, app_api_auth=auth)


def _slack(**extra):
    return KubeGuardianConfig.model_validate(
        {"target": {"namespace": "dev"}, "slack": {"alert_target": "C0123456789", **extra}}
    )


def test_slack_owners_and_mention_are_optional_with_defaults():
    slack = _slack().slack
    assert slack.owners == {} and slack.mention == ["critical"]


def test_slack_owners_accepts_user_ids():
    slack = _slack(owners={"agent": ["U0123456789", "W0123456789"]}).slack
    assert slack.owners == {"agent": ["U0123456789", "W0123456789"]}


@pytest.mark.parametrize(
    "user", ["@hong", "hong", "u0123456789", "U123", "C0123456789", "<@U0123456789>"]
)
def test_slack_owners_rejects_non_user_id(user):
    with pytest.raises(ValidationError, match="사용자 ID"):
        _slack(owners={"agent": [user]})


def test_slack_mention_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        _slack(mention=["info"])
