"""호출 체인의 순서·근거·종점을 검증한다."""

import pytest

from core.guard.judgement import judge
from core.guard.validate import validate_report
from service.agent.investigator.v1.coverage import find_gaps
from service.model import Evidence, Report


def report(chain=None):
    data = {
        "conclusion": "gateway 오류",
        "confidence": "high",
        "impact_scope": "전체",
        "evidence_ids": ["ev-1", "ev-2"],
        "issues": [
            {
                "rank": 1,
                "title": "오류",
                "impact": "core_path_broken",
                "confidence": "high",
                "evidence_ids": ["ev-1", "ev-2"],
                "cause_resource": {"kind": "Deployment", "name": "gateway"},
            }
        ],
        "hypotheses": [
            {"statement": "오류", "status": "supported", "evidence_ids": ["ev-1", "ev-2"]}
        ],
        "coverage": {
            a: {"status": "not_applicable", "summary": "해당 없음"}
            for a in ("run", "config", "wiring", "logs")
        },
    }
    if chain is not None:
        data["cause_chain"] = chain
    return Report.model_validate(data)


def hop(kind="Deployment", name="gateway", ids=None, downstream=False):
    return {
        "service": {"kind": kind, "name": name},
        "observation": "오류 관찰",
        "evidence_ids": ["ev-2"] if ids is None else ids,
        "error_from_downstream": downstream,
    }


STORE = {
    "ev-1": Evidence(
        kind="resource",
        subject="dev/Deployment/gateway",
        source="t",
        collected_at="2026-11-03T00:00:00Z",
        data={},
    ),
    "ev-2": Evidence(
        kind="log_excerpt",
        subject="dev/Pod/gateway-1",
        source="t",
        collected_at="2026-11-03T00:00:00Z",
        data={},
    ),
}


def test_historical_report_and_chain_round_trip():
    assert report().cause_chain == []
    r = report([hop(kind=k) for k in ("Pod", "Service", "Deployment")])
    assert Report.model_validate_json(r.model_dump_json()).cause_chain == r.cause_chain


def test_filter_citations_preserves_unsupported_hops_and_input():
    r = report([hop(ids=["ev-2", "ev-99"]), hop(ids=["ev-99"]), hop(ids=[])])
    before = r.model_dump()
    out = validate_report(r, STORE.__contains__)
    assert [h.evidence_ids for h in out.report.cause_chain] == [["ev-2"], [], []]
    assert "cause_chain.1" in out.demoted and "cause_chain.2" in out.demoted
    assert r.model_dump() == before


@pytest.mark.parametrize("chain", [[hop(kind="Service")], [hop(ids=[])], [hop(downstream=True)]])
def test_invalid_chain_rechecks_and_cannot_be_confirmed(chain):
    r = report(chain)
    assert any("호출 체인" in g for g in find_gaps(r, []))
    judged = judge(r, STORE.get)
    assert judged.judgement == "candidate"
    assert "호출 체인" in judged.judgement_basis


def test_unknown_citations_cannot_bypass_final_judgement():
    assert judge(report([hop(ids=["ev-99"])]), STORE.get).judgement == "candidate"


def test_intermediate_forwarder_and_valid_terminal_can_confirm():
    r = report([hop(kind="Service", name="agent", downstream=True), hop()])
    assert not any("호출 체인" in g for g in find_gaps(r, []))
    assert judge(r, STORE.get).judgement == "confirmed"


def test_invalid_chain_does_not_upgrade_guess():
    r = report([hop(downstream=True)]).model_copy(update={"conclusion_supported": False})
    assert judge(r, STORE.get).judgement == "guess"


def test_chain_without_cause_is_a_gap():
    r = report([hop()])
    r.issues[0].cause_resource = None
    assert any("호출 체인" in g for g in find_gaps(r, []))


def test_empty_chain_cannot_confirm_a_new_causal_claim():
    r = report([])
    assert any("호출 체인" in g for g in find_gaps(r, []))
    assert judge(r, STORE.get).judgement == "candidate"
