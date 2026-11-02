import re
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
        (
            "app_api",
            {
                "scenario": "chat",
                "failed_step": None,
                "steps": [
                    {
                        "step": 1,
                        "action": "GET",
                        "path": "/",
                        "status": 200,
                        "latency_ms": 5,
                        "ok": True,
                    }
                ],
            },
            False,
        ),
        (
            "app_api",
            {
                "scenario": "chat",
                "failed_step": 2,
                "steps": [
                    {
                        "step": 1,
                        "action": "GET",
                        "path": "/",
                        "status": 200,
                        "latency_ms": 5,
                        "ok": True,
                    },
                    {
                        "step": 2,
                        "action": "POST",
                        "path": "/c",
                        "status": 502,
                        "latency_ms": 9,
                        "ok": False,
                    },
                ],
            },
            True,
        ),
        ("app_api", {"scenario": "chat"}, False),
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
    def __init__(self, text="요약입니다.", error=None, usage=None):
        self.text, self.error, self.usage, self.calls = text, error, usage, 0

    def __call__(self, name):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.text, usage_metadata=self.usage)


def run_once(engine, llm):
    from service.evidence_summary_worker import run_once as run

    return run(engine, model="m", llm_factory=llm, clock=lambda: NOW)


def test_run_once_summarizes_one_target_and_never_calls_twice(engine):
    first = add_evidence(engine, None, kind="resource", data={})
    second = add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm()

    assert run_once(engine, llm) == "done"
    assert llm.calls == 1
    store = EvidenceSummaryStore(engine)
    assert store.get(second, NOW).status == "done"  # 최신순
    assert store.get(first, NOW) is None
    assert run_once(engine, llm) == "done" and run_once(engine, llm) is None
    assert llm.calls == 2


def test_failed_summary_is_not_retried(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm(error=RuntimeError("down"))

    assert run_once(engine, llm) == "failed"
    assert EvidenceSummaryStore(engine).get(ev_id, NOW).status == "failed"
    assert run_once(engine, llm) is None
    assert llm.calls == 1


def test_expired_pending_is_not_called_again(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    EvidenceSummaryStore(engine).claim(ev_id, "m", at(0))  # 90초 TTL이 지난 pending
    llm = FakeLlm()

    assert run_once(engine, llm) is None
    assert llm.calls == 0


def test_run_once_skips_target_claimed_by_another_process(engine):
    ev_id = add_evidence(engine, None, kind="resource", data={})
    EvidenceSummaryStore(engine).claim(ev_id, "m", NOW)
    llm = FakeLlm()

    assert run_once(engine, llm) is None
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

    assert run_once(engine, FakeLlm()) == "done"
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


def test_log_has_status_and_tokens_but_no_content(engine, capsys):
    add_evidence(engine, None, kind="resource", data={"password": "hunter2"})
    llm = FakeLlm(text="본문 secret-text", usage={"input_tokens": 120, "output_tokens": 30})
    run_once(engine, llm)
    err = capsys.readouterr().err
    assert re.search(r"근거 요약: ev-0001 done \d+\.\d초 입력 120 출력 30", err)
    assert "secret-text" not in err and "hunter2" not in err


def test_log_marks_failure_and_missing_tokens(engine, capsys):
    add_evidence(engine, None, kind="resource", data={})
    run_once(engine, FakeLlm(error=RuntimeError("x")))
    assert re.search(r"ev-0001 failed \d+\.\d초 입력 - 출력 -", capsys.readouterr().err)


def test_next_targets_skips_malformed_row_and_logs_type(engine, capsys):
    from sqlalchemy.orm import Session

    from core.db.tables import EvidenceRow
    from service.evidence_summary_worker import next_targets

    good = add_evidence(engine, None, kind="resource", data={})
    with Session(engine) as s, s.begin():
        s.add(
            EvidenceRow(kind="resource", subject="x", collected_at=at(30), source="s", data=["bad"])
        )
    assert next_targets(engine, NOW) == [good]
    assert "ValidationError" in capsys.readouterr().err


class Stop(Exception):
    pass


def loop(engine, llm, stop_after):
    from service.evidence_summary_worker import summary_loop

    delays = []

    def sleep(seconds):
        delays.append(seconds)
        if len(delays) >= stop_after:
            raise Stop

    with pytest.raises(Stop):
        summary_loop(engine, model="m", llm_factory=llm, clock=lambda: NOW, poll_s=5, sleep=sleep)
    return delays


def test_failures_back_off_exponentially_to_cap(engine):
    for _ in range(200):
        add_evidence(engine, None, kind="resource", data={})
    llm = FakeLlm(error=RuntimeError("down"))

    delays = loop(engine, llm, 8)

    assert delays == [60, 120, 240, 480, 900, 900, 900, 900]
    assert llm.calls == 8  # 대기 없이 소진되지 않는다


def test_success_waits_one_second_and_resets_backoff(engine):
    for _ in range(4):
        add_evidence(engine, None, kind="resource", data={})
    outcomes = iter([RuntimeError("down"), RuntimeError("down"), None, RuntimeError("down")])

    class Flaky(FakeLlm):
        def invoke(self, messages):
            err = next(outcomes)
            self.error = err
            return super().invoke(messages)

    delays = loop(engine, Flaky(), 4)

    assert delays == [60, 120, 1, 60]


def test_idle_loop_polls(engine):
    assert loop(engine, FakeLlm(), 2) == [5, 5]
