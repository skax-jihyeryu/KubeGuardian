"""규칙 목록(catalog.py)이 명세(docs/05-rules.md §2)와 일치하는지 확인한다."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from service.checks.detectors.catalog import CATALOG

RULES_MD = Path(__file__).resolve().parents[3] / "docs" / "05-rules.md"
NATURE = {"상태": "state", "선언": "declared", "시간": "time"}
SEVERITY = {"🔴": "critical", "🟠": "warning", "⚪": "info"}


def _spec_rows() -> list[list[str]]:
    text = RULES_MD.read_text()
    section = text.split("## 2. 규칙 요약", 1)[1].split("\n## 3.", 1)[0]
    rows = [line for line in section.splitlines() if re.match(r"\| \**[NPWSCH]\d\d", line)]
    return [[cell.strip().replace("**", "") for cell in row.strip("|").split("|")] for row in rows]


def _hold(text: str) -> timedelta | None:
    if m := re.match(r"(\d+)분 지속", text):
        return timedelta(minutes=int(m[1]))
    if m := re.match(r"(\d+)시간 지속", text):
        return timedelta(hours=int(m[1]))
    assert text.startswith("즉시"), text
    return None


SPEC = {row[0]: row for row in _spec_rows()}


def test_catalog_has_same_rule_ids_as_spec():
    assert set(CATALOG) == set(SPEC)


@pytest.mark.parametrize("rule_id", sorted(SPEC))
def test_rule_matches_spec(rule_id):
    _, name, nature, hold, severity, _, runbooks = SPEC[rule_id]
    rule = CATALOG[rule_id]

    assert rule.name == name.replace(" (확장 후보에서 승격)", "")
    assert rule.nature == NATURE[nature]
    assert rule.hold == _hold(hold)
    assert rule.severities == tuple(SEVERITY[c] for c in severity if c in SEVERITY)
    assert rule.runbooks == tuple(r.strip() for r in runbooks.split(",") if r.strip() != "—")


def test_hygiene_rules_are_advice_and_others_are_signals():
    grades = {rule_id: rule.grade for rule_id, rule in CATALOG.items()}

    assert [r for r, g in grades.items() if g == "advice"] == ["H01", "H02"]
    assert sum(g == "signal" for g in grades.values()) == 11
