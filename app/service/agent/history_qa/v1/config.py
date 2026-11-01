"""history_qa v1 설정: 역할별 모델·예산·프롬프트 위치."""

from pathlib import Path

from core.guard.budget import Budget
from service.agent.base import AgentSpec

SPEC = AgentSpec(
    name="history_qa",
    version="v1",
    models={"answer": "gpt-6.1-sol"},
    budget=Budget(per_issue_tool_calls=8, total_tool_calls=8, seconds=60),
    prompts_dir=Path(__file__).parent / "prompts",
)
