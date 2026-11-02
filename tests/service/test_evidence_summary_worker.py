from datetime import timedelta

import pytest

from core.db import create_db_engine
from core.db.evidence_summaries import EvidenceSummaryStore
from service.model import Evidence
from tests.api.seed import add_evidence, at

NOW = at(3600)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


def ev(kind, data):
    return Evidence(kind=kind, subject="dev/Pod/x", collected_at=at(0), source="s", data=data)


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        ("resource", {}, True),
        ("log_excerpt", {"lines": []}, True),
        ("event", {"count": 0}, True),
        ("probe_result", {"ok": 1}, True),
        ("app_api", {"scenario": "chat", "failed_step": None, "steps": []}, True),
        ("app_api", {"status": 502, "latency_ms": 20}, True),
        ("app_api", {"status": None, "error": "timeout"}, True),
        ("app_api", {"status": 200, "latency_ms": 20}, False),
        ("app_api", {}, False),
        ("app_api", {"status": "bad"}, False),
        ("detector", {"rule_id": "S04", "verdict": "detected"}, True),
        ("detector", {"rule_id": "S04", "verdict": "observing"}, False),
        ("detector", {"rule_id": "H01", "verdict": "detected"}, False),
        ("detector", {"rule_id": "ZZZ", "verdict": "detected"}, False),
        ("detector", {}, False),
        ("detector", {"rule_id": ["x"], "verdict": "detected"}, False),
    ],
)
def test_is_presummary_target(kind, data, expected):
    from service.evidence_summary_worker import is_presummary_target

    assert is_presummary_target(ev(kind, data)) is expected


def test_next_targets_filters_by_age_summary_and_kind(engine):
    from service.evidence_summary_worker import next_targets

    old = add_evidence(engine, None, kind="resource", data={})
    fresh = add_evidence(engine, None, kind="resource", data={})
    done = add_evidence(engine, None, kind="resource", data={})
    skipped = add_evidence(engine, None, kind="app_api", data={"status": 200})
    EvidenceSummaryStore(engine).claim(done, "m", NOW)
    # old: 24시간 밖으로 만든다
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from core.db.tables import EvidenceRow

    with Session(engine) as s, s.begin():
        s.execute(
            update(EvidenceRow)
            .where(EvidenceRow.id == 1)
            .values(collected_at=at(0) - timedelta(days=2))
        )
    assert old == "ev-0001"
    assert next_targets(engine, NOW) == [fresh]
    assert skipped not in next_targets(engine, NOW)


def test_next_targets_newest_first_and_skips_nontargets(engine):
    from service.evidence_summary_worker import next_targets

    target = add_evidence(engine, None, kind="resource", data={})
    for _ in range(5):
        add_evidence(engine, None, kind="app_api", data={"status": 200})
    assert next_targets(engine, NOW, limit=1) == [target]
    second = add_evidence(engine, None, kind="resource", data={})
    assert next_targets(engine, NOW, limit=1) == [second]
