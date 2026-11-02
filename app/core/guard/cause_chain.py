"""호출 체인의 근거와 종점을 검사한다. 오류 전달 여부 자체는 LLM 판단이라는 한계가 있다."""

from collections.abc import Callable

from service.model import Report


def cause_chain_gaps(report: Report, *, known: Callable[[str], bool] | None = None) -> list[str]:
    chain = report.cause_chain
    top = report.top_issue
    if not chain:
        return ["호출 체인이 없습니다"] if top else []
    gaps = []
    if top is None or chain[-1].service != top.cause_resource:
        gaps.append("호출 체인의 마지막 리소스가 1순위 원인 리소스와 다릅니다")
    for index, hop in enumerate(chain, 1):
        if not hop.evidence_ids or (known and not any(known(i) for i in hop.evidence_ids)):
            gaps.append(f"호출 체인 {index}번째 지점에 유효한 근거가 없습니다")
    if chain[-1].error_from_downstream:
        gaps.append(
            "호출 체인의 마지막 지점이 하위 서비스 오류를 전달했습니다. 오류 출처를 확인하세요"
        )
    return gaps
