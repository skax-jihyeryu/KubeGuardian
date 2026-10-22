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
        "slack: {alert_target: a, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critcal: {first_after: 30m, max: 3}}}",
        "slack: {alert_target: a, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critical: {first_after: 30분, max: 3}}}",
        "slack: {alert_target: a, bot_token_secret: b, app_token_secret: c,"
        " reminders: {critical: {first_after: 30m, max: 0}}}",
        "schedule: nope",
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
