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


def _hyp(status, evidence_ids, *, hypothesis_id="h1", resource="gateway", **extra):
    return {
        "id": hypothesis_id,
        "resource": {"kind": "Deployment", "name": resource},
        "statement": "가설",
        "status": status,
        "evidence_ids": list(evidence_ids),
        "support": [{"evidence_id": e, "observation": "직접 관찰"} for e in evidence_ids],
        **extra,
    }


DEFAULT_HYPOTHESES = [_hyp("supported", ["ev-1", "ev-2"])]


def _report(
    evidence_ids,
    issue_ids=(),
    supported=True,
    issues=True,
    cause=("Deployment", "gateway"),
    hypotheses=DEFAULT_HYPOTHESES,
    hypothesis_id="h1",
):
    issue = {
        "rank": 1,
        "hypothesis_id": hypothesis_id,
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
        "원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태) · 연결된 지지 가설 근거 2종"
    )


def _basis(**kw):
    out = judge(_report(["ev-1", "ev-2"], **kw), LOOKUP)
    return out.judgement, out.judgement_basis


def test_missing_or_unsupported_linked_hypothesis_is_candidate():
    assert _basis(hypotheses=[]) == (
        "candidate",
        "원인 리소스 설정 인용 · 근거 출처 2종(로그, 설정·상태) · 1순위 원인과 연결된 가설 없음",
    )
    for status in ("refuted", "inconclusive"):
        judgement, basis = _basis(hypotheses=[_hyp(status, ["ev-1", "ev-2"])])
        assert judgement == "candidate" and basis.endswith("연결된 가설의 지지 근거 부족")


def test_supported_hypothesis_with_one_kind_is_candidate():
    judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1"])])

    assert judgement == "candidate" and basis.endswith("연결된 지지 가설 근거 1종")


def test_failed_or_missing_support_is_not_counted():
    for other in ("ev-5", "ev-99"):
        out = judge(
            _report(["ev-1", "ev-2", other], hypotheses=[_hyp("supported", ["ev-1", other])]),
            LOOKUP,
        )
        assert out.judgement == "candidate" and out.judgement_basis.endswith("근거 1종")


def test_unlinked_hypotheses_do_not_supply_support():
    first = _hyp("supported", ["ev-1"])
    for evidence in (["ev-2"], ["ev-1", "ev-2"]):
        other = _hyp("supported", evidence, hypothesis_id="h2")
        judgement, basis = _basis(hypotheses=[first, other])
        assert judgement == "candidate" and basis.endswith("근거 1종")


def test_same_kind_cited_many_times_in_hypothesis_is_one_kind():
    out = judge(
        _report(
            ["ev-1", "ev-2", "ev-3", "ev-6"],
            hypotheses=[_hyp("supported", ["ev-1", "ev-3", "ev-6"])],
        ),
        LOOKUP,
    )
    assert out.judgement == "candidate" and out.judgement_basis.endswith("근거 1종")


def test_duplicate_id_and_different_cause_do_not_resolve_link():
    first = _hyp("supported", ["ev-1", "ev-2"])
    different = _hyp("supported", ["ev-1", "ev-2"], resource="agent")
    for hypotheses in ([first, different], [different]):
        judgement, basis = _basis(hypotheses=hypotheses)
        assert judgement == "candidate" and basis.endswith("연결된 가설 없음")


def test_refutation_or_unknown_blocks_confirmation():
    for extra in (
        {"refute": [{"evidence_id": "ev-4", "observation": "같은 요청의 상충 기록"}]},
        {"unknown": [{"question": "동일 요청의 오류 생성 위치", "reason": "요청 식별자 없음"}]},
    ):
        judgement, basis = _basis(hypotheses=[_hyp("supported", ["ev-1", "ev-2"], **extra)])
        assert judgement == "candidate" and "미해결" in basis


def test_uncited_cause_config_stays_candidate_with_two_kind_hypothesis():
    out = judge(_report(["ev-3", "ev-4"]), LOOKUP)

    assert out.judgement == "candidate"
    assert out.judgement_basis.endswith("원인 리소스 설정 지지 근거 없음")


def test_unsupported_conclusion_is_guess_even_with_supported_hypothesis():
    assert judge(_report([], supported=False), LOOKUP).judgement == "guess"


def test_issue_evidence_counts_toward_sources():
    report = _report(["ev-1"], issue_ids=["ev-4"], hypotheses=[_hyp("supported", ["ev-1", "ev-4"])])
    assert judge(report, LOOKUP).judgement == "confirmed"


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


def test_legacy_report_with_no_explicit_link_is_not_newly_confirmed():
    legacy = _report(
        ["ev-1", "ev-2"],
        hypothesis_id=None,
        hypotheses=[
            {"statement": "주소 오류", "status": "supported", "evidence_ids": ["ev-1", "ev-2"]}
        ],
    )
    assert judge(legacy, LOOKUP).judgement == "candidate"
    saved = legacy.model_copy(update={"judgement": "confirmed"}).model_dump_json(exclude_none=True)
    assert Report.model_validate_json(saved).judgement == "confirmed"


def test_caller_receipt_with_no_same_request_origin_stays_candidate():
    hypothesis = _hyp(
        "supported",
        ["ev-1", "ev-2"],
        unknown=[
            {
                "question": "게이트웨이가 동일 요청의 429를 만들었는가",
                "reason": "호출자 수신 기록과 서버 200을 잇는 요청 식별자가 없음",
            }
        ],
    )
    report = _report(["ev-1", "ev-2"], hypotheses=[hypothesis])
    report.cause_chain[-1].error_from_downstream = True
    assert judge(report, LOOKUP).judgement == "candidate"
    assert "오류를 전달" in judge(report, LOOKUP).judgement_basis


def test_terminal_hop_must_share_usable_support():
    report = _report(["ev-1", "ev-2", "ev-4"])
    report.cause_chain[-1].evidence_ids = ["ev-4"]

    out = judge(report, LOOKUP)

    assert out.judgement == "candidate"
    assert out.judgement_basis.endswith("공통 지지 근거 없음")
