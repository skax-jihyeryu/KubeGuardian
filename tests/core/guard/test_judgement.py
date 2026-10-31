from core.guard.judgement import judge
from service.model import Report

SOURCES = {
    "ev-1": "설정·상태",
    "ev-2": "로그",
    "ev-3": "설정·상태",
    "ev-4": "이벤트",
}


def _report(evidence_ids, issue_ids=(), supported=True, issues=True):
    return Report.model_validate(
        {
            "conclusion": "agent selector 오타",
            "confidence": "high",
            "impact_scope": "채팅 전체",
            "conclusion_supported": supported,
            "evidence_ids": list(evidence_ids),
            "issues": [
                {
                    "rank": 1,
                    "title": "selector 오타",
                    "impact": "core_path_broken",
                    "confidence": "high",
                    "evidence_ids": list(issue_ids),
                }
            ]
            if issues
            else [],
        }
    )


def test_fetched_cause_and_two_kinds_of_evidence_is_confirmed():
    out = judge(_report(["ev-1", "ev-2"]), cause_fetched=True, sources=SOURCES)

    assert out.judgement == "confirmed"
    assert out.judgement_basis == "원인 리소스 직접 조회 · 근거 출처 2종(로그, 설정·상태)"


def test_issue_evidence_counts_toward_sources():
    out = judge(_report(["ev-1"], issue_ids=["ev-4"]), cause_fetched=True, sources=SOURCES)

    assert out.judgement == "confirmed"


def test_one_kind_of_evidence_is_candidate_even_if_cited_many_times():
    out = judge(_report(["ev-1", "ev-3"]), cause_fetched=True, sources=SOURCES)

    assert out.judgement == "candidate" and "근거 출처 1종(설정·상태)" in out.judgement_basis


def test_unfetched_cause_is_candidate():
    out = judge(_report(["ev-1", "ev-2"]), cause_fetched=False, sources=SOURCES)

    assert out.judgement == "candidate" and out.judgement_basis.startswith("원인 리소스 미조회")


def test_unsupported_conclusion_is_guess():
    out = judge(_report([], supported=False), cause_fetched=True, sources=SOURCES)

    assert (out.judgement, out.judgement_basis) == ("guess", "결론에 근거 없음")


def test_no_top_issue_is_guess():
    out = judge(_report(["ev-1", "ev-2"], issues=False), cause_fetched=False, sources=SOURCES)

    assert out.judgement == "guess"


def test_evidence_of_unknown_source_is_not_counted():
    # 실패한 도구 호출 등 출처를 모르는 근거는 세지 않는다
    out = judge(_report(["ev-1", "ev-x"]), cause_fetched=True, sources=SOURCES)

    assert out.judgement == "candidate"


def test_llm_confidence_is_kept_as_is():
    assert judge(_report(["ev-1"]), cause_fetched=False, sources=SOURCES).confidence == "high"
