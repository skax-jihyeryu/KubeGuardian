"""investigator v1 설정: 역할별 모델·예산·프롬프트 위치.

모델은 우선 gpt-6.1-sol 하나로 둔다 (2026-10-19 사용자 결정).
최종 조합은 비교 실험(실행 계획서 §4 #8)으로 정한다.
모델별 API 제약(temperature, Responses API)은 core/llm.py의 MODEL_OPTIONS가 처리한다.
"""

from pathlib import Path

from core.guard.budget import Budget
from service.agent.base import AgentSpec

SPEC = AgentSpec(
    name="investigator",
    version="v1",
    models={
        "triage": "gpt-6.1-sol",
        "plan": "gpt-6.1-sol",
        "investigate": "gpt-6.1-sol",
        "report": "gpt-6.1-sol",
    },
    budget=Budget(),
    prompts_dir=Path(__file__).parent / "prompts",
)
