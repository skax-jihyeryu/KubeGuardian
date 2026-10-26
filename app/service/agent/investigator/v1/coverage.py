"""멈춤 조건 검사 (spec §3.2): 보고서가 나온 뒤 코드가 빈틈을 찾는다. LLM은 쓰지 않는다.

- 1순위 원인 리소스의 설정을 `get_resource`로 직접 봤는가
- 구동·설정·연결·로그 영역을 실제로 조회했는가 (도구 호출 기록 기준)
"""

from service.agent.investigator.v1.state import Observation
from service.model import AREAS, Area, Report

AREA_BY_TOOL: dict[str, set[Area]] = {
    "get_events": {"run"},
    "get_pod_logs": {"logs"},
    "get_topology": {"wiring"},
    "probe": {"wiring"},
    "run_api_scenario": {"wiring"},
    "query_app_api": {"wiring"},
}
AREA_BY_KIND: dict[str, set[Area]] = {
    "Pod": {"run"},
    "Node": {"run"},
    "Deployment": {"run", "config"},
    "ReplicaSet": {"config"},
    "ConfigMap": {"config"},
    "Secret": {"config"},
    "Service": {"wiring"},
    "EndpointSlice": {"wiring"},
}
_LABEL: dict[Area, str] = {"run": "구동", "config": "설정", "wiring": "연결", "logs": "로그"}


def observed_areas(observations: list[Observation]) -> set[Area]:
    areas: set[Area] = set()
    for o in observations:
        if not o.ok:
            continue
        if o.tool == "get_resource":
            areas |= AREA_BY_KIND.get(o.kind or "", set())
        else:
            areas |= AREA_BY_TOOL.get(o.tool, set())
    return areas


def _fetched(observations: list[Observation], kind: str, name: str) -> bool:
    """이름을 지정해 조회한 경우만 센다 (라벨 셀렉터 조회는 특정 리소스를 본 것이 아니다)."""
    return any(
        o.ok
        and o.tool == "get_resource"
        and (o.kind or "").lower() == kind.lower()
        and o.name == name
        for o in observations
    )


def _cause_gap(report: Report, observations: list[Observation]) -> str | None:
    """1순위 원인 리소스를 지정하지 않았거나 이름으로 직접 조회하지 않았으면 그 사유."""
    if not report.issues:
        return None
    cause = min(report.issues, key=lambda i: i.rank).cause_resource
    if cause is None:
        return "1순위 원인 리소스를 지정하지 않았습니다"
    if not _fetched(observations, cause.kind, cause.name):
        ref = f"{cause.kind}/{cause.name}"
        return f"원인 리소스 {ref}의 설정을 get_resource로 직접 조회하지 않았습니다"
    return None


def find_gaps(report: Report, observations: list[Observation]) -> list[str]:
    gaps = []
    if cause_gap := _cause_gap(report, observations):
        gaps.append(cause_gap)
    seen = observed_areas(observations)
    for area in AREAS:
        if area not in seen and getattr(report.coverage, area).status != "not_applicable":
            gaps.append(f"{_LABEL[area]} 영역을 확인하지 않았습니다")
    return gaps


def mark_unobserved(report: Report, observations: list[Observation], reason: str) -> Report:
    """조회한 적 없는 영역을 확인함으로 적었으면 확인하지 못함으로 내린다 (근거 번호도 비운다)."""
    seen = observed_areas(observations)
    coverage = report.coverage
    for area in AREAS:
        cov = getattr(coverage, area)
        if area not in seen and cov.status == "checked":
            update = {"status": "not_checked", "summary": reason, "evidence_ids": []}
            coverage = coverage.model_copy(update={area: cov.model_copy(update=update)})
    return report.model_copy(update={"coverage": coverage})


def downgrade_unresolved_cause(report: Report, observations: list[Observation]) -> Report:
    """끝까지 원인 리소스 spec을 못 봤으면 high 확신도를 medium으로 낮추고 설정 영역에 적는다."""
    if _cause_gap(report, observations) is None:
        return report
    top = min(report.issues, key=lambda i: i.rank)
    issues = [
        i.model_copy(update={"confidence": "medium"}) if i is top and i.confidence == "high" else i
        for i in report.issues
    ]
    config = report.coverage.config
    note = "원인 리소스 spec 미조회"
    config = config.model_copy(
        update={"summary": f"{config.summary} / {note}" if config.summary else note}
    )
    return report.model_copy(
        update={
            "issues": issues,
            "confidence": "medium" if report.confidence == "high" else report.confidence,
            "coverage": report.coverage.model_copy(update={"config": config}),
        }
    )
