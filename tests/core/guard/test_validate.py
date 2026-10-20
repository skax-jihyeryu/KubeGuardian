from core.guard.validate import validate_report
from service.model import Report
from service.model.report import Fact, Hypothesis, Issue

KNOWN = {"ev-1", "ev-2"}


def known(ev_id: str) -> bool:
    return ev_id in KNOWN


def _report(**overrides) -> Report:
    values = {
        "conclusion": "gateway 주소 env가 localhost",
        "confidence": "high",
        "impact_scope": "전체",
        "evidence_ids": ["ev-1"],
    }
    values.update(overrides)
    return Report(**values)


def test_unknown_evidence_ids_are_dropped():
    report = _report(
        evidence_ids=["ev-1", "ev-9"],
        issues=[
            Issue(
                rank=1,
                title="t",
                impact="core_path_broken",
                confidence="high",
                evidence_ids=["ev-2", "ev-8"],
            )
        ],
    )

    out = validate_report(report, known)

    assert out.report.evidence_ids == ["ev-1"]
    assert out.report.issues[0].evidence_ids == ["ev-2"]


def test_fact_without_evidence_is_demoted():
    report = _report(
        preliminary_facts=[
            Fact(statement="Pod 3개 Ready", evidence_ids=["ev-1"]),
            Fact(statement="엔드포인트 0개", evidence_ids=["ev-7"]),
        ]
    )

    out = validate_report(report, known)

    assert [f.statement for f in out.report.preliminary_facts] == ["Pod 3개 Ready"]
    assert out.demoted == ["엔드포인트 0개"]


def test_hypothesis_without_evidence_becomes_inconclusive():
    report = _report(
        hypotheses=[
            Hypothesis(
                statement="selector 오타",
                status="supported",
                reason="라벨 불일치",
                evidence_ids=["ev-5"],
            )
        ]
    )

    out = validate_report(report, known)

    h = out.report.hypotheses[0]
    assert (h.status, h.evidence_ids) == ("inconclusive", [])
    assert h.reason.startswith("(추정) ")
    assert out.demoted == ["selector 오타"]


def test_conclusion_without_evidence_is_flagged():
    assert validate_report(_report(evidence_ids=["ev-9"]), known).conclusion_supported is False
    assert validate_report(_report(), known).conclusion_supported is True
