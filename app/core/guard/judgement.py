"""판단 수준: 보고서 결론을 무엇으로 확인했나를 코드가 정한다 (#106, 기획서 §6.2).

LLM이 매긴 확신도(confidence)는 기준이 없고 정답률과 맞는지 잰 적이 없어 화면·알림에 쓰지 않는다.
운영자가 "조치를 바로 할지, 먼저 확인할지" 정할 수 있게 아래 규칙으로 판단 수준을 붙인다.

| 값 | 조건 |
|---|---|
| confirmed (확정) | 1순위 원인 리소스의 설정을 직접 조회 + 결론 근거의 출처가 2종 이상 |
| candidate (후보) | 결론 근거는 있으나 위 조건 중 하나가 빠짐 |
| guess (추정) | 결론에 근거가 없음(검증기 강등) 또는 1순위 문제가 없음 |

출처는 근거를 만든 도구로 묶는다. 같은 종류를 여러 번 인용해도 1종이다. 출처를 모르는 근거
(실패한 도구 호출 등)는 세지 않는다.
"""

from collections.abc import Mapping

from service.model import Report

CONFIG = "설정·상태"
LOG = "로그"
EVENT = "이벤트"
PROBE = "연결 확인"
API = "앱 API"

SOURCE_OF_TOOL: dict[str, str] = {
    "get_resource": CONFIG,
    "get_topology": CONFIG,
    "get_pod_logs": LOG,
    "get_events": EVENT,
    "probe": PROBE,
    "run_api_scenario": API,
    "query_app_api": API,
}
SIGNAL_SOURCE = CONFIG  # 규칙 판정(신호판)은 리소스 설정·상태를 본 것이다
SYMPTOM_SOURCE = API  # 점검 때 실패한 API 시나리오

CONFIRM_MIN_SOURCES = 2


def judge(report: Report, *, cause_fetched: bool, sources: Mapping[str, str]) -> Report:
    """판단 수준과 그 설명을 붙인 보고서. LLM 확신도는 그대로 둔다 (W4 평가에서 비교용)."""
    top = report.top_issue
    if not report.conclusion_supported:
        return _with(report, "guess", "결론에 근거 없음")
    if top is None:
        return _with(report, "guess", "1순위 문제 없음")
    cited = set(report.evidence_ids) | set(top.evidence_ids)
    kinds = sorted({sources[i] for i in cited if i in sources})
    if not kinds:
        return _with(report, "guess", "결론 근거의 출처를 확인할 수 없음")
    basis = " · ".join(
        [
            "원인 리소스 직접 조회" if cause_fetched else "원인 리소스 미조회",
            f"근거 출처 {len(kinds)}종({'·'.join(kinds)})",
        ]
    )
    confirmed = cause_fetched and len(kinds) >= CONFIRM_MIN_SOURCES
    return _with(report, "confirmed" if confirmed else "candidate", basis)


def _with(report: Report, judgement: str, basis: str) -> Report:
    return report.model_copy(update={"judgement": judgement, "judgement_basis": basis})
