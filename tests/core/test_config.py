from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import load_kubeguardian_config

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
