"""scenarios/*/expected.yaml이 채점기가 읽는 형식을 지키는지 검사한다 (오타 방지)."""

from pathlib import Path

import pytest
import yaml

from service.checks.detectors.catalog import CATALOG

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "scenarios").glob("*/expected.yaml"))
ALLOWED_KEYS = {
    "id",
    "level",
    "k8s_visible",
    "gate_path",
    "must_signals",
    "allowed_signals",
    "forbidden_signals",
    "must_check",
    "not_a_cause",
    "root_cause",
    "affected_services",
    "expected_top1",
}
MUST_CHECK_KEYS = {"run", "config", "wiring", "logs"}


def test_expected_files_are_found():
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name)
def test_expected_file_uses_known_keys_and_signals(path):
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(expected) <= ALLOWED_KEYS
    assert expected["id"] == path.parent.name or path.parent.name == "_template"
    for key in ("must_signals", "allowed_signals", "forbidden_signals"):
        for rule_id in expected.get(key) or []:
            assert rule_id in CATALOG or (rule_id == "*" and key == "forbidden_signals"), (
                f"{key}: {rule_id}"
            )
    assert set(expected.get("must_check") or {}) <= MUST_CHECK_KEYS
