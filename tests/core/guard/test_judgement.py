from datetime import UTC, datetime

from core.guard.judgement import judge
from service.model import Evidence, Report

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def _ev(kind, subject):
    return Evidence(kind=kind, subject=subject, collected_at=NOW, source="t", data={})


STORE = {
    "ev-1": _ev("resource", "dev/Deployment/gateway"),  # 원인 리소스 설정
    "ev-2": _ev("log_excerpt", "dev/Pod/gateway-1/gateway"),
    "ev-3": _ev("detector", "dev/Service/agent"),
    "ev-4": _ev("event", "dev/Event/*"),
    "ev-5": _ev("probe_result", "dev/Tool/probe"),  # 조회 실패 기록
    "ev-6": _ev("resource", "dev/Service/agent"),
}
LOOKUP = STORE.get


DEFAULT_HYPOTHESES = [
    {"statement": "gateway 주소 오류", "status": "supported", "evidence_ids": ["ev-1", "ev-2"]}
]


def _hyp(status, evidence_ids):
    return {"statement": "가설", "status": status, "evidence_ids": list(evidence_ids)}


def _report(
    evidence_ids,
    issue_ids=(),
    supported=True,
    issues=True,
    cause=("Deployment", "gateway"),
    hypotheses=DEFAULT_HYPOTHESES,
):
    issue = {
        "rank": 1,
        "title": "gateway 주소 설정 오류",
        "impact": "core_path_broken",
        "confidence": "high",
        "evidence_ids": list(issue_ids),
    }
    if cause:
        issue["cause_resource"] = {"kind": cause[0], "name": cause[1]}
    return Report.model_validate(
        {
            "conclusion": "gateway 주소 env가 localhost",
            "confidence": "high",
            "impact_scope": "전체",
            "conclusion_supported": supported,
            "evidence_ids": list(evidence_ids),
            "issues": [issue] if issues else [],
            "hypotheses": list(hypotheses),
            "cause_chain": [
                {
                    "service": {"kind": cause[0], "name": cause[1]},
                    "observation": "설정 확인",
                    "evidence_ids": ["ev-1"],
                    "error_from_downstream": False,
                }
            ]
            if cause
            else [],
        }
    )


def test_cited_cause_config_and_two_kinds_is_confirmed():
    out = judge(_report(["ev-1", "ev-2"]), LOOKUP)

    assert out.judgement == "confirmed"
    assert out.judgement_basis == (
        "원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태) · 지지 가설 근거 2종"
    )


def _basis(**kw):
    out = judge(_report(["ev-1", "ev-2"], **kw), LOOKUP)
    return out.judgement, out.judgement_basis


def test_no_supported_hypothesis_is_candidate():
    for hyps in ([], [_hyp("refuted", ["ev-1", "ev-2"])], [_hyp("inconclusive", ["ev-1", "ev-2"])]):
        judgement, basis = _basis(hypotheses=hyps)
        assert judgement == "candidate" and basis.endswith(" · 지지된 가설 없음")


def test_supported_hypothesis_with_one_kind_is_candidate():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1"])])

    assert judgement == "candidate" and basis.endswith(" · 지지 가설 근거 최대 1종")


def test_supported_hypothesis_citing_only_failure_records_is_candidate():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-5"])])

    assert judgement == "candidate" and basis.endswith(" · 지지 가설 근거 최대 0종")


def test_sources_of_different_supported_hypotheses_are_not_merged():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1"]), _hyp("supported", ["ev-2"])])

    assert judgement == "candidate" and basis.endswith("최대 1종")


def test_same_kind_cited_many_times_in_hypothesis_is_one_kind():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1", "ev-3", "ev-6"])])

    assert judgement == "candidate" and basis.endswith("최대 1종")


def test_any_supported_hypothesis_with_two_kinds_is_enough():
    out = judge(
        _report(
            ["ev-1", "ev-2"],
            hypotheses=[_hyp("supported", ["ev-1"]), _hyp("supported", ["ev-1", "ev-4"])],
        ),
        LOOKUP,
    )

    assert out.judgement == "confirmed" and out.judgement_basis.endswith(" · 지지 가설 근거 2종")


def test_hypothesis_evidence_not_found_by_lookup_is_not_counted():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1", "ev-99"])])

    assert judgement == "candidate" and basis.endswith("최대 1종")


def test_uncited_cause_config_stays_candidate_with_two_kind_hypothesis():
    out = judge(_report(["ev-3", "ev-4"]), LOOKUP)

    assert out.judgement == "candidate" and out.judgement_basis.endswith(" · 지지 가설 근거 2종")


def test_unsupported_conclusion_is_guess_even_with_supported_hypothesis():
    assert judge(_report([], supported=False), LOOKUP).judgement == "guess"


def test_issue_evidence_counts_toward_sources():
    assert judge(_report(["ev-1"], issue_ids=["ev-4"]), LOOKUP).judgement == "confirmed"


def test_one_kind_cited_many_times_is_candidate():
    out = judge(_report(["ev-1", "ev-3", "ev-6"]), LOOKUP)

    assert out.judgement == "candidate" and "근거 출처 1종(설정·상태)" in out.judgement_basis


def test_uncited_cause_config_is_candidate_even_with_two_kinds():
    # 원인 리소스를 조회했더라도 결론이 그 설정을 인용하지 않으면 확정이 아니다
    out = judge(_report(["ev-3", "ev-4"]), LOOKUP)

    assert out.judgement == "candidate"
    assert out.judgement_basis.startswith("원인 리소스 설정 미인용")


def test_no_cause_resource_is_candidate():
    assert judge(_report(["ev-1", "ev-2"], cause=None), LOOKUP).judgement == "candidate"


def test_unsupported_conclusion_is_guess():
    out = judge(_report([], supported=False), LOOKUP)

    assert (out.judgement, out.judgement_basis) == ("guess", "결론에 근거 없음")


def test_no_top_issue_is_guess():
    assert judge(_report(["ev-1", "ev-2"], issues=False), LOOKUP).judgement == "guess"


def test_only_failure_records_cited_is_guess_with_that_reason():
    out = judge(_report(["ev-5"]), LOOKUP)

    assert (out.judgement, out.judgement_basis) == ("guess", "결론 근거가 조회 실패 기록뿐")


def test_failure_record_is_not_counted_as_a_source():
    assert judge(_report(["ev-1", "ev-5"]), LOOKUP).judgement == "candidate"


def test_llm_confidence_is_kept_as_is():
    assert judge(_report(["ev-1"]), LOOKUP).confidence == "high"


def test_judgement_fields_are_not_in_llm_schema():
    props = Report.model_json_schema()["properties"]

    assert "judgement" not in props and "judgement_basis" not in props
