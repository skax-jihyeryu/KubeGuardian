"""AI 조사 결과 채점: 보고서 결론이 시나리오의 정답 원인과 같은지 (기획서 §9).

조사 모델과 다른 모델(gpt-4.1)로 판정한다. 사람이 결과를 나란히 보고 최종 확인한다
(W2 사용자 결정. 본인 채점 vs LLM 채점 비교는 비교 실험 4).
"""

import json
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from core.guard.redact import redact_text
from service.agent._common.structured import invoke_structured

JUDGE_MODEL = "gpt-4.1"

PROMPT = """당신은 Kubernetes 장애 조사 보고서의 채점자입니다.
보고서의 결론이 정답 원인과 **같은 원인**을 짚었는지만 판정합니다. 표현·상세 정도는 달라도
됩니다. 단, 원인 리소스와 그 안의 설정 항목(이미지 참조, env, selector, quota·응답 등)이나
동작이 모두 정답과 같아야 같은 원인입니다. 리소스 이름만 같거나 같은 리소스의 다른 설정을
짚었으면 false입니다. 롤아웃 정체, Pod 대기 같은 상태 서술은 원인이 아니라 증상입니다.
증상만 말하고 원인 대상을 짚지 못했거나 다른 원인을 1순위로 올렸어도 false입니다.
같은 주입이 만든 서로 다른 오류 문구는 같은 원인으로 봅니다.
설정 항목이 없는 원인(서비스 사이 연결 차단, 하위·외부 서비스의 오류 응답)은
원인 대상과 경로(출발→도착)·동작이 같으면 같은 원인입니다.

정답 원인: {root_cause}
정답 요약 1순위: {expected_top1}
보고서 결론: {conclusion}

JSON 하나로만 답합니다: {{"match": true, "reason": "한 문장"}}"""


class JudgeResult(BaseModel):
    match: bool | None  # None = 채점 실패
    reason: str = ""


def judge_top1(conclusion: str, expected: dict[str, Any], llm: BaseChatModel) -> JudgeResult:
    text = PROMPT.format(
        root_cause=expected.get("root_cause", ""),
        expected_top1=expected.get("expected_top1", ""),
        conclusion=conclusion,
    )
    try:
        return invoke_structured(llm, JudgeResult, [("user", text)])
    except Exception as e:  # 채점 실패는 결과에 남기고 평가는 계속한다
        return JudgeResult(match=None, reason=redact_text(f"채점 실패: {type(e).__name__}: {e}"))


EVIDENCE_CHARS = 4000  # 근거 하나당 judge에 넣는 최대 글자 수 (직렬화한 data 기준)

CLAIM_PROMPT = """당신은 Kubernetes 장애 조사 보고서의 근거 채점자입니다.
아래 주장이 인용한 근거 원문만 보고, 주장이 근거로 뒷받침되는지 판정합니다.
근거에 없는 내용을 추론으로 메우지 않습니다.
- supported: 주장의 핵심이 근거에 그대로 있다.
- partial: 일부만 근거에 있거나 근거가 간접적이다.
- unsupported: 근거에 없거나 근거와 어긋난다.
"기각한 가설:"로 시작하는 주장은 가설이 참인지가 아니라 근거가 그 기각을 뒷받침하는지 판정합니다.
근거가 가설과 어긋나면 supported입니다.

주장: {claim}

인용한 근거:
{evidence}

JSON 하나로만 답합니다: {{"verdict": "supported", "reason": "한 문장"}}"""

Verdict = Literal["supported", "partial", "unsupported"]


class Claim(BaseModel):
    source: str  # "conclusion" | "issue" | "hypothesis:<index>"
    text: str
    evidence_ids: list[str] = []


class ClaimJudgement(BaseModel):
    verdict: Verdict
    reason: str = ""


class ClaimVerdict(BaseModel):
    source: str
    verdict: Verdict | None  # None = 채점 실패
    reason: str = ""


def extract_claims(report: dict) -> list[Claim]:
    """채점할 주장: 결론, 1순위 문제, 판정이 난(supported·refuted) 가설."""
    claims = [
        Claim(
            source="conclusion",
            text=report["conclusion"],
            evidence_ids=report.get("evidence_ids") or [],
        )
    ]
    top = min(report.get("issues") or [], key=lambda i: i["rank"], default=None)
    if top is not None and top["rank"] == 1:
        claims.append(
            Claim(source="issue", text=top["title"], evidence_ids=top.get("evidence_ids") or [])
        )
    for i, h in enumerate(report.get("hypotheses") or []):
        if h["status"] in ("supported", "refuted"):
            text = f"{h['statement']} / {h.get('reason', '')}"
            if h["status"] == "refuted":
                text = f"기각한 가설: {h['statement']} / 기각 이유: {h.get('reason', '')}"
            claims.append(
                Claim(
                    source=f"hypothesis:{i}",
                    text=text,
                    evidence_ids=h.get("evidence_ids") or [],
                )
            )
    return claims


def _render_evidence(ev: dict) -> str:
    data = json.dumps(ev["data"], ensure_ascii=False)[:EVIDENCE_CHARS]
    return f"[{ev['id']}] {ev['kind']} {ev['subject']}\n{data}"


def judge_claims(
    claims: list[Claim], evidence: dict[str, dict], llm: BaseChatModel
) -> list[ClaimVerdict]:
    verdicts = []
    for claim in claims:
        found = [evidence[i] for i in claim.evidence_ids if i in evidence]
        if not found:
            verdicts.append(
                ClaimVerdict(source=claim.source, verdict="unsupported", reason="인용 근거 없음")
            )
            continue
        text = CLAIM_PROMPT.format(
            claim=claim.text, evidence="\n\n".join(_render_evidence(e) for e in found)
        )
        try:
            r = invoke_structured(llm, ClaimJudgement, [("user", text)])
            verdicts.append(ClaimVerdict(source=claim.source, verdict=r.verdict, reason=r.reason))
        except Exception as e:  # 그 주장만 채점 실패로 남긴다
            reason = redact_text(f"채점 실패: {type(e).__name__}: {e}")
            verdicts.append(ClaimVerdict(source=claim.source, verdict=None, reason=reason))
    return verdicts


def support_summary(verdicts: list[ClaimVerdict]) -> dict:
    """partial은 0.5. 채점 실패(None)는 분모에서 뺀다."""
    score = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}
    judged = [score[v.verdict] for v in verdicts if v.verdict is not None]
    conclusion = next((v.verdict for v in verdicts if v.source == "conclusion"), None)
    return {
        "support_rate": sum(judged) / len(judged) if judged else None,
        "conclusion_support": conclusion,
    }
