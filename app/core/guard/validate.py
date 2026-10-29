"""보고서 근거 검증 (상세설계 §2.1 Step 3, §6). 결정적 코드다.

보고서가 인용한 근거 번호가 실제로 저장된 근거인지 확인한다.
- 없는 근거 번호는 지운다.
- 근거가 하나도 남지 않은 사실은 사실 목록에서 빼고(Fact는 근거 1개 이상 필수) `demoted`에만 남긴다.
  보고서 본문에는 나오지 않는다.
- 근거가 빈 가설(지어낸 번호만 있던 경우와 처음부터 빈 경우 모두)은 supported·refuted를
  판단 보류(inconclusive)로 바꾸고 문장 앞에 "(추정)"을 붙인다.
- 근거가 빈 문제는 신뢰도를 low로 내리고 제목 앞에 "(추정)"을 붙인다.
- 영역별 coverage는 근거를 거른다. 확인함(checked)인데 남은 근거가 없으면 확인하지 못함으로 내리고
  `demoted`에 `coverage.<영역>`을 남긴다. 확인하지 못함·해당 없음인데 사유가 비면 "(사유 미기재)".
- 결론의 근거가 없으면 conclusion_supported=False. 호출한 쪽이 보고서를 1회 다시 쓰게 한다.
"""

from collections.abc import Callable

from pydantic import BaseModel

from service.model import AREAS, ChatAnswer, Report

_ESTIMATE = "(추정) "


def _estimate(text: str) -> str:
    return text if text.startswith(_ESTIMATE) else _ESTIMATE + text


class ValidationOutcome(BaseModel):
    report: Report
    demoted: list[str] = []  # 근거가 없어 추정으로 내린 문장
    conclusion_supported: bool


def validate_report(report: Report, known: Callable[[str], bool]) -> ValidationOutcome:
    def keep(ids: list[str]) -> list[str]:
        return [i for i in ids if known(i)]

    demoted: list[str] = []
    facts = []
    for fact in report.preliminary_facts:
        ids = keep(fact.evidence_ids)
        if ids:
            facts.append(fact.model_copy(update={"evidence_ids": ids}))
        else:
            demoted.append(fact.statement)

    hypotheses = []
    for h in report.hypotheses:
        ids = keep(h.evidence_ids)
        if not ids and h.status != "inconclusive":
            demoted.append(h.statement)
            h = h.model_copy(update={"status": "inconclusive", "statement": _estimate(h.statement)})
        hypotheses.append(h.model_copy(update={"evidence_ids": ids}))

    issues = []
    for i in report.issues:
        ids = keep(i.evidence_ids)
        if not ids:
            demoted.append(i.title)
            i = i.model_copy(update={"confidence": "low", "title": _estimate(i.title)})
        issues.append(i.model_copy(update={"evidence_ids": ids}))
    coverage = report.coverage
    for area in AREAS:
        cov = getattr(coverage, area)
        ids = keep(cov.evidence_ids)
        update: dict = {"evidence_ids": ids}
        if cov.status == "checked" and not ids:
            demoted.append(f"coverage.{area}")
            update.update(status="not_checked", summary="(근거 없음) " + cov.summary)
        elif cov.status != "checked" and not cov.summary.strip():
            update["summary"] = "(사유 미기재)"
        coverage = coverage.model_copy(update={area: cov.model_copy(update=update)})
    evidence_ids = keep(report.evidence_ids)
    cleaned = report.model_copy(
        update={
            "evidence_ids": evidence_ids,
            "preliminary_facts": facts,
            "hypotheses": hypotheses,
            "issues": issues,
            "coverage": coverage,
        }
    )
    return ValidationOutcome(
        report=cleaned, demoted=demoted, conclusion_supported=bool(evidence_ids)
    )


_ESTIMATE_SUFFIX = " (추정)"


def validate_answer(
    answer: ChatAnswer,
    *,
    evidence: Callable[[str], bool],
    issue: Callable[[int], bool],
    run: Callable[[int], bool],
) -> ChatAnswer:
    """없는 인용 번호를 지우고, 인용이 안 남은 사실·해석 끝에 "(추정)"을 붙인다."""
    sentences = []
    for s in answer.sentences:
        ev_ids = [i for i in s.evidence_ids if evidence(i)]
        issue_ids = [i for i in s.issue_ids if issue(i)]
        run_ids = [i for i in s.run_ids if run(i)]
        text = s.text
        if s.kind != "guide" and not (ev_ids or issue_ids or run_ids):
            text = text if text.endswith(_ESTIMATE_SUFFIX) else text + _ESTIMATE_SUFFIX
        sentences.append(
            s.model_copy(
                update={
                    "text": text,
                    "evidence_ids": ev_ids,
                    "issue_ids": issue_ids,
                    "run_ids": run_ids,
                }
            )
        )
    return answer.model_copy(update={"sentences": sentences})
