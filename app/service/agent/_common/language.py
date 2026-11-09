"""보고서 산문의 언어 검사: 한자(중국어)가 섞였는지 본다 (#230).

LLM이 쓰는 문장 필드만 본다. 근거 인용·리소스 이름·로그 발췌(preliminary_facts 등)는 보지 않는다.
한글(Hangul)은 해당하지 않고, CJK 통합 한자·확장 A·호환 한자만 잡는다.
한계: 산문이 로그 원문을 인용해 중국어가 들어 있으면 재작성 후에도 경고만 남는다.
"""

import re

from service.model import Report

_HAN = re.compile(r"[㐀-䶿一-鿿]")

KOREAN_ONLY_NOTE = (
    "위 보고서 문장에 한자(중국어)가 섞였습니다. 모든 문장을 한국어로 다시 작성하세요. "
    "구조·근거 ID·내용은 그대로 두고 한자 문장만 한국어로 바꿉니다. 같은 JSON 형식으로만 답합니다."
)


def has_han(text: str) -> bool:
    return _HAN.search(text) is not None


def cited_ids(report: Report) -> set[str]:
    """보고서가 인용한 근거 번호 전체."""
    ids = set(report.evidence_ids)
    for group in (report.issues, report.hypotheses, report.cause_chain):
        for item in group:
            ids.update(item.evidence_ids)
    return ids


def han_fields(report: Report) -> list[str]:
    """한자가 들어간 산문 필드 이름 목록. 없으면 빈 목록."""
    prose: dict[str, str] = {
        "conclusion": report.conclusion,
        "impact_scope": report.impact_scope,
        "verification": report.verification,
    }
    for i, issue in enumerate(report.issues):
        prose[f"issues[{i}].title"] = issue.title
    for i, h in enumerate(report.hypotheses):
        prose[f"hypotheses[{i}].statement"] = h.statement
        prose[f"hypotheses[{i}].reason"] = h.reason
    for i, hop in enumerate(report.cause_chain):
        prose[f"cause_chain[{i}].observation"] = hop.observation
    for i, action in enumerate(report.actions):
        prose[f"actions[{i}]"] = action
    for area in ("run", "config", "wiring", "logs"):
        prose[f"coverage.{area}.summary"] = getattr(report.coverage, area).summary
    return [name for name, text in prose.items() if has_han(text)]
