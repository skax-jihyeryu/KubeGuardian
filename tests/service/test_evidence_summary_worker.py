from datetime import timedelta
from types import SimpleNamespace

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


# ── 작업자 스레드 ──


class FakeLlm:
    def __init__(self, text="요약입니다.", error=None):
        self.text, self.error, self.calls = text, error, 0

    def __call__(self, name):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.text)


def run_once(engine, llm):
    from service.evidence_summary_worker import run_once as run

    return run(engine, model="m", llm_factory=llm, clock=lambda: NOW)


def test_run_once_summarizes_one_target_and_never_calls_twice(engine):
    first = add_evidence(engine, None, kind="resource", data={})
    second = add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm()

    assert run_once(engine, llm) == 1
    assert llm.calls == 1
    store = EvidenceSummaryStore(engine)
    assert store.get(second, NOW).status == "done"  # 최신순
    assert store.get(first, NOW) is None
    assert run_once(engine, llm) == 1 and run_once(engine, llm) == 0
    assert llm.calls == 2


def test_failed_summary_is_not_retried(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm(error=RuntimeError("down"))

    assert run_once(engine, llm) == 1
    assert EvidenceSummaryStore(engine).get(ev_id, NOW).status == "failed"
    assert run_once(engine, llm) == 0
    assert llm.calls == 1


def test_expired_pending_is_not_called_again(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    EvidenceSummaryStore(engine).claim(ev_id, "m", at(0))  # 90초 TTL이 지난 pending
    llm = FakeLlm()

    assert run_once(engine, llm) == 0
    assert llm.calls == 0


def test_run_once_skips_target_claimed_by_another_process(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    EvidenceSummaryStore(engine).claim(ev_id, "m", NOW)
    llm = FakeLlm()

    assert run_once(engine, llm) == 0
    assert llm.calls == 0


def test_exception_on_one_target_does_not_block_the_next(engine, monkeypatch, capsys):
    from service import evidence_summary_worker as worker

    good = add_evidence(engine, None, kind="resource", data={})
    bad = add_evidence(engine, None, kind="resource", data={})
    real = worker.summarize_evidence

    def flaky(engine, ev_id, **kwargs):
        if ev_id == bad:
            raise OSError("password=hunter2")
        return real(engine, ev_id, **kwargs)

    monkeypatch.setattr(worker, "summarize_evidence", flaky)

    assert run_once(engine, FakeLlm()) == 1
    assert EvidenceSummaryStore(engine).get(good, NOW).status == "done"
    err = capsys.readouterr()
    assert "OSError" in err.err + err.out and "hunter2" not in err.err + err.out


def test_summary_thread_is_daemon_and_generates(engine):
    import time

    from service.evidence_summary_worker import start_summary_thread

    ev_id = add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm()
    # collected_at(at(30))이 24시간 안에 들도록 시계를 고정한다
    thread = start_summary_thread(engine, llm, poll_s=0.01, clock=lambda: NOW)
    assert thread.daemon and thread.is_alive()
    for _ in range(300):
        summary = EvidenceSummaryStore(engine).get(ev_id, NOW)
        if summary and summary.status == "done":
            break
        time.sleep(0.01)
    assert summary and summary.status == "done"
