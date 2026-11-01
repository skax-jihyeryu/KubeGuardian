"""scenarios/*/expected.yaml이 채점기가 읽는 형식을 지키는지 검사한다 (오타 방지)."""

from pathlib import Path
from typing import get_args

import pytest
import yaml

from service.checks.detectors.catalog import CATALOG
from service.model import EvidenceKind

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
    "expected_cause",
    "key_evidence",
    "root_cause",
    "affected_services",
    "expected_top1",
}
MUST_CHECK_KEYS = {"run", "config", "wiring", "logs"}
EVIDENCE_KINDS = set(get_args(EvidenceKind))
EVAL_SCENARIOS = {"A1", "A3", "A5", "B1", "C1", "C2"}  # 층별 채점 대상 (spec §3)


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


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name)
def test_new_eval_keys_have_valid_shape(path):
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))
    causes = expected.get("expected_cause") or []
    keys = expected.get("key_evidence") or []

    for cause in causes:
        assert cause.get("kind") and cause.get("name"), cause
    ids = [k["id"] for k in keys]
    assert len(ids) == len(set(ids))
    for key in keys:
        assert isinstance(key["obtainable"], bool), key["id"]
        assert key.get("desc")
        assert key["any_of"], key["id"]
        for pattern in key["any_of"]:
            assert pattern["kind"] in EVIDENCE_KINDS, pattern
            contains = pattern.get("contains")
            assert contains is None or isinstance(contains, str | list)
    for item in expected.get("not_a_cause") or []:
        if isinstance(item, str):
            continue
        assert item["text"]
        assert set(item) <= {"text", "resource"}
        if "resource" in item:
            assert item["resource"]["kind"] and item["resource"]["name"]
    if path.parent.name in EVAL_SCENARIOS:
        assert causes
        assert keys
