"""러너의 2단계 채점(score_layers)과 종료 코드·CLI·주입 대상 읽기. 가짜 judge와 tmp DB로 돈다."""

from datetime import UTC, datetime

import pytest

import run as run_mod
from core.db import EvidenceStore, create_db_engine
from judge import ClaimVerdict
from run import (
    EXIT_FAIL,
    EXIT_INVALID,
    EXIT_PASS,
    decide_exit_runs,
    main,
    read_target,
    score_layers,
)
from service.model import Evidence

EXPECTED = {
    "expected_top1": "llm-gateway 이미지 오타",
    "expected_cause": [{"kind": "Deployment", "name": "llm-gateway"}],
    "key_evidence": [
        {
            "id": "image",
            "obtainable": True,
            "any_of": [{"kind": "resource", "subject": "*llm-gateway*", "contains": "badimg"}],
        }
    ],
}


def _db(tmp_path, *, with_key=True):
    db = tmp_path / "kg.db"
    store = EvidenceStore(create_db_engine(str(db)))

    def add(subject, data):
        return store.add(
            Evidence(
                kind="resource",
                subject=subject,
                collected_at=datetime.now(UTC),
                source="GET /x",
                data=data,
            )
        )

    key_id = add("dev/Deployment/llm-gateway", {"image": "badimg" if with_key else "ok"})
    return db, key_id


def _report(ev_id):
    return {
        "conclusion": "llm-gateway 이미지가 잘못됐다",
        "evidence_ids": [ev_id],
        "issues": [{"rank": 1, "title": "이미지 오타", "evidence_ids": [ev_id]}],
        "hypotheses": [],
    }


def _ai(cause="llm-gateway", judge_match=True, status="done"):
    return {
        "status": status,
        "judge_match": judge_match,
        "passed": False,
        "legacy_passed": judge_match is True,
        "eval": {"cause_match": cause == "llm-gateway", "forbidden_cause": False},
    }


def _judge(verdict="supported"):
    return lambda claims, ev: [ClaimVerdict(source=c.source, verdict=verdict) for c in claims]


def _score(ai, report, steps, db, judge=None, expected=EXPECTED):
    return score_layers(ai, report, steps, expected, db_path=db, claim_judge=judge or _judge())


def test_score_layers_classifies_f3_when_key_found_but_wrong_cause(tmp_path):
    db, ev = _db(tmp_path)
    steps = [{"node": "tools", "evidence_ids": [ev]}]

    ai = _score(_ai(cause="agent", judge_match=False), _report(ev), steps, db)

    assert ai["failure"] == "F3" and ai["passed"] is False
    assert ai["eval"]["key_all_found"] is True and ai["eval"]["cause_match"] is False
    assert ai["support_rate"] == 1.0 and ai["conclusion_support"] == "supported"
    assert [c["source"] for c in ai["claims"]] == ["conclusion", "issue"]


def test_score_layers_pass_requires_supported_conclusion(tmp_path):
    db, ev = _db(tmp_path)
    steps = [{"node": "tools", "evidence_ids": [ev]}]

    ok = _score(_ai(), _report(ev), steps, db)
    bad = _score(_ai(), _report(ev), steps, db, judge=_judge("unsupported"))

    assert ok["failure"] == "pass" and ok["passed"] is True
    assert bad["failure"] == "F5" and bad["passed"] is False


def test_missing_db_makes_run_invalid_f0(tmp_path):
    ai = _score(_ai(), _report("ev-0001"), [], tmp_path / "none.db")

    assert ai["invalid"] == "근거 DB 없음" and ai["failure"] == "F0" and ai["passed"] is False


def test_cited_id_missing_from_db_is_invalid_f0(tmp_path):
    db, ev = _db(tmp_path)

    ai = _score(_ai(), _report("ev-9999"), [{"node": "tools", "evidence_ids": [ev]}], db)

    assert ai["invalid"] == "근거 DB 없음" and ai["failure"] == "F0"


def test_unjudged_claim_or_judge_is_invalid_f0(tmp_path):
    db, ev = _db(tmp_path)
    steps = [{"node": "tools", "evidence_ids": [ev]}]

    claim_none = _score(_ai(), _report(ev), steps, db, judge=_judge(None))
    judge_none = _score(_ai(judge_match=None), _report(ev), steps, db)

    assert claim_none["failure"] == "F0" and claim_none["invalid"]
    assert judge_none["failure"] == "F0" and judge_none["invalid"]


def test_missing_key_evidence_is_f2(tmp_path):
    db, ev = _db(tmp_path, with_key=False)
    steps = [{"node": "tools", "evidence_ids": [ev]}]

    ai = _score(_ai(cause="agent", judge_match=False), _report(ev), steps, db)

    assert ai["failure"] == "F2" and ai["eval"]["key_missing"] == ["image"]


def test_failed_agent_run_is_error_not_invalid(tmp_path):
    ai = _score(_ai(status="failed"), None, [], tmp_path / "none.db")

    assert ai["failure"] == "error" and "invalid" not in ai and ai["passed"] is False
    assert decide_exit_runs(True, [ai, {"passed": True}]) == EXIT_FAIL


def test_gate_c_runs_are_left_as_decided(tmp_path):
    skipped = {"status": "skipped", "passed": True, "legacy_passed": True}
    ran = {"status": "done", "passed": False, "fail_reason": "기대 게이트 C인데 조사가 실행됐다"}

    assert "failure" not in _score(skipped, None, [], tmp_path / "x.db")
    assert _score(skipped, None, [], tmp_path / "x.db")["passed"] is True
    assert "failure" not in _score(ran, None, [], tmp_path / "x.db")


def test_decide_exit_runs_any_invalid_is_2():
    assert decide_exit_runs(True, [{"passed": True}, {"passed": False, "invalid": "x"}]) == (
        EXIT_INVALID
    )
    assert decide_exit_runs(True, [{"passed": True}, {"passed": True}]) == EXIT_PASS
    assert decide_exit_runs(True, [{"passed": True}, {"passed": False}]) == EXIT_FAIL
    assert decide_exit_runs(False, [{"passed": True}]) == EXIT_FAIL


def test_ai_repeats_must_be_positive():
    with pytest.raises(SystemExit):
        main(argv=["A1", "--ai-repeats", "0"])


def test_read_target_strips_and_handles_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "SCENARIOS", tmp_path)
    assert read_target("B1") is None
    state = tmp_path / "B1" / ".state"
    state.mkdir(parents=True)
    (state / "target").write_text("  llm-gateway \nextra\n")
    assert read_target("B1") == "llm-gateway"
    (state / "target").write_text("\n")
    assert read_target("B1") is None


def test_incomplete_run_records_limited(tmp_path):
    db, ev = _db(tmp_path, with_key=False)
    steps = [{"node": "tools", "evidence_ids": [ev]}]

    cut = _score(_ai(cause="agent", judge_match=False, status="incomplete"), _report(ev), steps, db)
    done = _score(_ai(cause="agent", judge_match=False), _report(ev), steps, db)

    assert cut["limited"] is True and cut["failure"] == "F2"
    assert "limited" not in done and done["failure"] == "F2"
