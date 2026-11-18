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


C1_STORE = {
    "ev-10": _ev("log_excerpt", "dev/Pod/agent-1/agent"),  # agent가 하위 호출에서 429를 받음
    "ev-11": _ev("resource", "dev/Deployment/agent"),
    "ev-12": _ev("log_excerpt", "dev/Pod/llm-gateway-1/llm-gateway"),  # 같은 요청 ID의 429 생성
    "ev-13": _ev("resource", "dev/Deployment/llm-gateway"),
}


def _c1_report(resource, support, unknown=()):
    hypothesis = {
        "id": "h1",
        "resource": {"kind": "Deployment", "name": resource},
        "statement": f"{resource}가 429를 만들었다",
        "status": "supported",
        "evidence_ids": [e for e, _ in support],
        "support": [{"evidence_id": e, "observation": o} for e, o in support],
        "unknown": list(unknown),
    }
    report = _report(
        [e for e, _ in support], cause=("Deployment", resource), hypotheses=[hypothesis]
    )
    report.cause_chain[-1].evidence_ids = [e for e, _ in support]
    return report


def test_c1_caller_receipt_does_not_confirm_internal_generation():
    """#147 회귀: agent의 429 수신 로그는 수신 사실일 뿐 agent 내부 생성의 근거가 아니다."""
    report = _c1_report(
        "agent",
        [("ev-10", "agent가 llm-gateway 호출에서 429를 받음")],
        unknown=[{"question": "같은 요청의 429 생성 지점", "reason": "요청 식별자가 로그에 없음"}],
    )
    out = judge(report, C1_STORE.get)
    assert out.judgement == "candidate"
    assert "미확인" in out.judgement_basis


def test_c1_receipt_without_config_support_stays_candidate_even_without_unknown():
    report = _c1_report("agent", [("ev-10", "agent가 429를 받음")])
    out = judge(report, C1_STORE.get)
    assert out.judgement == "candidate"
    assert "설정 지지 근거 없음" in out.judgement_basis


def test_c1_same_request_origin_with_config_is_confirmed():
    """같은 요청의 생성 로그와 원인 리소스 설정을 직접 본 경우만 확정한다."""
    report = _c1_report(
        "llm-gateway",
        [
            ("ev-12", "같은 요청 ID에서 llm-gateway가 429를 생성"),
            ("ev-13", "llm-gateway rate limit 설정"),
        ],
    )
    assert judge(report, C1_STORE.get).judgement == "confirmed"


def test_saved_v1_json_without_link_fields_loads_and_is_judged():
    """#181 이전 저장본: id·support·hypothesis_id가 없는 JSON을 그대로 읽는다."""
    saved = (
        '{"conclusion": "c", "confidence": "high", "impact_scope": "x",'
        ' "evidence_ids": ["ev-1", "ev-2"],'
        ' "issues": [{"rank": 1, "title": "t", "impact": "core_path_broken",'
        ' "confidence": "high", "evidence_ids": [],'
        ' "cause_resource": {"kind": "Deployment", "name": "gateway"}}],'
        ' "hypotheses": [{"statement": "h", "status": "supported", "evidence_ids": ["ev-1"]}],'
        ' "judgement": "confirmed", "judgement_basis": "옛 기준"}'
    )
    report = Report.model_validate_json(saved)
    h = report.hypotheses[0]
    assert (h.id, h.support, h.refute, h.unknown) == (None, [], [], [])
    assert report.judgement == "confirmed"  # 저장된 판정은 다시 매기지 않는다
    assert report.linked_hypothesis is None
