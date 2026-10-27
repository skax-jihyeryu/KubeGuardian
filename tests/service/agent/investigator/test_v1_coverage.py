"""멈춤 조건 검사: 원인 리소스 설정을 직접 봤는지, 네 영역을 봤는지 (코드가 정한다)."""

from service.agent.investigator.v1.coverage import (
    downgrade_unresolved_cause,
    find_gaps,
    mark_unobserved,
    observed_areas,
)
from service.agent.investigator.v1.state import Observation
from service.model import Report
from service.model.report import AreaCoverage, Coverage, Issue, ResourceRef

ALL_NA = {
    a: AreaCoverage(status="not_applicable", summary="해당 없음")
    for a in ("run", "config", "wiring", "logs")
}


def _issue(kind="Deployment", name="gateway", rank=1) -> Issue:
    return Issue(
        rank=rank,
        title="t",
        impact="core_path_broken",
        confidence="high",
        evidence_ids=["ev-1"],
        cause_resource=ResourceRef(kind=kind, name=name) if kind else None,
    )


def _report(issues=None, **areas) -> Report:
    cov = Coverage(**{**ALL_NA, **areas})
    return Report(
        conclusion="c", confidence="high", impact_scope="s", issues=issues or [], coverage=cov
    )


def _get(kind, name, ev="ev-2") -> Observation:
    return Observation(evidence_id=ev, tool="get_resource", kind=kind, name=name)


def test_find_gaps_missing_cause_resource():
    gaps = find_gaps(_report([_issue(kind=None)]), [])

    assert gaps == ["1순위 원인 리소스를 지정하지 않았습니다"]


def test_find_gaps_cause_resource_not_fetched():
    gaps = find_gaps(_report([_issue()]), [])

    assert gaps == [
        "원인 리소스 Deployment/gateway의 설정을 get_resource로 직접 조회하지 않았습니다"
    ]


def test_find_gaps_cause_resource_case_insensitive():
    assert find_gaps(_report([_issue(kind="deployment")]), [_get("Deployment", "gateway")]) == []


def test_find_gaps_label_selector_lookup_does_not_count():
    gaps = find_gaps(_report([_issue()]), [_get("Deployment", None)])

    assert len(gaps) == 1 and "get_resource로 직접 조회하지 않았습니다" in gaps[0]


def test_find_gaps_uses_top_ranked_issue():
    gaps = find_gaps(
        _report([_issue(rank=2, name="other"), _issue(rank=1)]), [_get("Deployment", "gateway")]
    )

    assert gaps == []


def test_not_applicable_area_is_not_a_gap():
    assert find_gaps(_report(), []) == []


def test_unobserved_area_is_a_gap_unless_not_applicable():
    report = _report(config=AreaCoverage(status="checked", summary="s", evidence_ids=["ev-1"]))

    assert find_gaps(report, []) == ["설정 영역을 확인하지 않았습니다"]
    assert find_gaps(report, [_get("ConfigMap", "x")]) == []


def test_observed_areas_follow_tool_and_kind():
    obs = [
        Observation(evidence_id="ev-1", tool="get_events"),
        Observation(evidence_id="ev-2", tool="get_pod_logs"),
        _get("Deployment", "gateway"),
        _get("Service", "gateway"),
    ]

    assert observed_areas(obs) == {"run", "config", "wiring", "logs"}
    assert observed_areas([Observation(evidence_id="ev-1", tool="probe")]) == {"wiring"}


def test_mark_unobserved_downgrades_checked():
    report = _report(
        config=AreaCoverage(status="checked", summary="env 확인", evidence_ids=["ev-1"]),
        logs=AreaCoverage(status="checked", summary="로그 확인", evidence_ids=["ev-3"]),
    )
    obs = [Observation(evidence_id="ev-3", tool="get_pod_logs")]

    out = mark_unobserved(report, obs, "확인하지 못함 (예산 소진)")

    assert out.coverage.config.status == "not_checked"
    assert out.coverage.config.summary == "확인하지 못함 (예산 소진)"
    assert out.coverage.logs.status == "checked"
    assert out.coverage.run.status == "not_applicable"


def test_failed_get_resource_does_not_satisfy_cause_check():
    failed = _get("Deployment", "gateway").model_copy(update={"ok": False})

    gaps = find_gaps(_report([_issue()]), [failed])

    assert len(gaps) == 1 and "get_resource로 직접 조회하지 않았습니다" in gaps[0]


def test_failed_probe_does_not_cover_wiring():
    failed = Observation(evidence_id="ev-1", tool="probe", ok=False)

    assert observed_areas([failed]) == set()
    assert observed_areas([failed.model_copy(update={"ok": True})]) == {"wiring"}


def test_mark_unobserved_clears_evidence_ids_of_downgraded_area():
    report = _report(config=AreaCoverage(status="checked", summary="s", evidence_ids=["ev-1"]))

    out = mark_unobserved(report, [], "확인하지 못함 (예산 소진)")

    assert out.coverage.config.evidence_ids == []


def test_unresolved_cause_lowers_confidence_and_marks_config():
    report = _report(
        [_issue()], config=AreaCoverage(status="checked", summary="env 확인", evidence_ids=["ev-1"])
    )

    out = downgrade_unresolved_cause(report, [])

    assert out.confidence == "medium" and out.issues[0].confidence == "medium"
    assert out.coverage.config.summary == "env 확인 / 원인 리소스 spec 미조회"
    assert out.coverage.config.status == "checked"


def test_unresolved_cause_only_lowers_high_and_top_issue():
    top = _issue().model_copy(update={"confidence": "low"})
    second = _issue(rank=2, name="x")
    report = _report([top, second]).model_copy(update={"confidence": "medium"})

    out = downgrade_unresolved_cause(report, [])

    assert out.confidence == "medium"
    assert [i.confidence for i in out.issues] == ["low", "high"]


def test_resolved_cause_leaves_report_unchanged():
    report = _report([_issue()])

    assert downgrade_unresolved_cause(report, [_get("Deployment", "gateway")]) == report


def test_missing_cause_resource_counts_as_unresolved():
    out = downgrade_unresolved_cause(_report([_issue(kind=None)]), [])

    assert out.confidence == "medium"


def test_no_issues_skips_cause_check():
    assert find_gaps(_report([]), []) == []
