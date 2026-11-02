"""판단 수준: 보고서 결론을 무엇으로 확인했나를 코드가 정한다 (#106, 기획서 §6.2).

LLM이 매긴 확신도(confidence)는 기준이 없고 정답률과 맞는지 잰 적이 없어 화면·알림에 쓰지 않는다.
운영자가 "조치를 바로 할지, 먼저 확인할지" 정할 수 있게 아래 규칙으로 판단 수준을 붙인다.

| 값 | 조건 |
|---|---|
| confirmed (확정) | 원인 리소스 설정 인용 + 근거 출처 2종+ + supported 가설 하나의 근거 출처 2종+ |
| candidate (후보) | 결론 근거는 있으나 위 조건 중 하나가 빠짐 (지지된 가설 없음 포함) |
| guess (추정) | 결론에 근거가 없음(검증기 강등), 1순위 문제가 없음, 인용이 조회 실패 기록뿐 |

에이전트 버전과 상관없이 같은 기준이 되도록 조사 실행 공통 경로(run_request)가 저장된 근거로 정한다.
출처는 근거 종류로 묶는다. 같은 종류를 여러 번 인용해도 1종이고, 조회 실패 기록은 세지 않는다.
가설의 출처는 가설 하나 안에서만 센다 (여러 가설의 출처를 합치지 않는다).
한계: 가설과 원인 리소스의 연결이 스키마에 없어 무관한 가설이 supported여도 통과한다.
"""

from collections.abc import Callable, Iterable

from service.model import Evidence, Report

CONFIG = "설정·상태"
SOURCE_OF_KIND: dict[str, str] = {
    "resource": CONFIG,
    "detector": CONFIG,  # 규칙 판정(신호판)은 리소스 설정·상태를 본 것이다
    "log_excerpt": "로그",
    "event": "이벤트",
    "probe_result": "연결 확인",
    "app_api": "앱 API",
}
CONFIRM_MIN_SOURCES = 2

EvidenceLookup = Callable[[str], Evidence | None]


def judge(report: Report, lookup: EvidenceLookup) -> Report:
    """판단 수준과 그 설명을 붙인 보고서. LLM 확신도는 그대로 둔다 (W4 평가에서 비교용)."""
    top = report.top_issue
    if not report.conclusion_supported:
        return _with(report, "guess", "결론에 근거 없음")
    if top is None:
        return _with(report, "guess", "1순위 문제 없음")
    ids = {*report.evidence_ids, *top.evidence_ids}
    cited = [e for i in ids if (e := lookup(i))]
    usable = _usable(ids, lookup)
    if not usable:
        reason = "결론 근거가 조회 실패 기록뿐" if cited else "결론 근거를 찾을 수 없음"
        return _with(report, "guess", reason)
    kinds = sorted(_sources(usable))
    cause = top.cause_resource
    cause_cited = cause is not None and any(
        e.kind == "resource" and e.subject.endswith(f"/{cause.kind}/{cause.name}") for e in usable
    )
    # 가설 하나 안에서 출처가 2종이어야 한다 (여러 가설의 출처를 합치지 않는다)
    supported = [h for h in report.hypotheses if h.status == "supported"]
    best = max((len(_sources(_usable(h.evidence_ids, lookup))) for h in supported), default=0)
    if not supported:
        hypothesis_basis = "지지된 가설 없음"
    elif best < CONFIRM_MIN_SOURCES:
        hypothesis_basis = f"지지 가설 근거 최대 {best}종"
    else:
        hypothesis_basis = f"지지 가설 근거 {best}종"
    basis = " · ".join(
        [
            "원인 리소스 설정 인용" if cause_cited else "원인 리소스 설정 미인용",
            f"근거 출처 {len(kinds)}종({', '.join(kinds)})",
            hypothesis_basis,
        ]
    )
    confirmed = cause_cited and len(kinds) >= CONFIRM_MIN_SOURCES and best >= CONFIRM_MIN_SOURCES
    return _with(report, "confirmed" if confirmed else "candidate", basis)


def _usable(ids: Iterable[str], lookup: EvidenceLookup) -> list[Evidence]:
    """lookup으로 찾은 근거 중 출처로 셀 수 있는 것 (조회 실패 기록·알 수 없는 종류 제외)."""
    found = (e for i in set(ids) if (e := lookup(i)))
    return [e for e in found if not _is_failure(e) and e.kind in SOURCE_OF_KIND]


def _sources(evidence: Iterable[Evidence]) -> set[str]:
    return {SOURCE_OF_KIND[e.kind] for e in evidence}


def _is_failure(evidence: Evidence) -> bool:
    """도구 호출 실패 기록 (Tool.run이 <ns>/Tool/<도구>로 남긴다)."""
    return "/Tool/" in evidence.subject


def _with(report: Report, judgement: str, basis: str) -> Report:
    return report.model_copy(update={"judgement": judgement, "judgement_basis": basis})
