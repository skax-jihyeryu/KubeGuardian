"""AI 조사 결과 채점: 보고서 결론이 시나리오의 정답 원인과 같은지 (기획서 §9).

조사 모델과 다른 모델(gpt-4.1)로 판정한다. 사람이 결과를 나란히 보고 최종 확인한다
(W2 사용자 결정. 본인 채점 vs LLM 채점 비교는 비교 실험 4).
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from core.guard.redact import redact_text
from service.agent._common.structured import invoke_structured

JUDGE_MODEL = "gpt-4.1"

PROMPT = """당신은 Kubernetes 장애 조사 보고서의 채점자입니다.
보고서의 결론이 정답 원인과 **같은 원인**을 짚었는지만 판정합니다. 표현·상세 정도가 달라도
원인 대상(리소스·설정)이 같으면 같은 원인입니다. 같은 주입이 만든 서로 다른 오류 문구
(예: 레지스트리 주소 해석 실패와 이미지 받기 실패)는 같은 원인으로 봅니다. 증상만 말하고
원인 대상(리소스·설정)을 짚지 못했으면 false이고, 다른 원인을 1순위로 올렸어도 false입니다.

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
