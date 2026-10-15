"""에이전트 공통 인터페이스 (실행 계획서 §1.3).

에이전트는 `service/agent/<이름>/v<N>/`에 두고 BaseAgent를 상속해 registry에 등록한다.
LLM·도구는 생성자로 주입한다. 그래서 테스트는 가짜 LLM으로 같은 코드를 돌린다.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import AwareDatetime, BaseModel

from service.model import InvestigationRequest, Report

AgentStatus = Literal["done", "incomplete", "failed"]  # 완료 / 상한 도달(부분 결과) / 실패


class Budget(BaseModel):
    """조사 상한 (기획서 §6.5). 분기는 프롬프트가 아니라 코드가 강제한다."""

    per_issue_tool_calls: int = 8
    total_tool_calls: int = 20
    seconds: float = 150


class AgentSpec(BaseModel):
    name: str
    version: str
    models: dict[str, str]  # 역할 → 모델 (예: triage → gpt-4.1-mini, investigate → gpt-4.1)
    budget: Budget = Budget()
    prompts_dir: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.name}:{self.version}"


class AgentStep(BaseModel):
    """조사 단계 하나 (보고서 화면 P5)."""

    node: str
    started_at: AwareDatetime
    detail: dict[str, Any] = {}


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    seconds: float = 0


class AgentResult(BaseModel):
    status: AgentStatus
    report: Report | None = None
    steps: list[AgentStep] = []
    usage: Usage = Usage()
    error: str | None = None  # failed일 때 사유


LlmFactory = Callable[[str], BaseChatModel]  # 모델 이름 → LLM (core.llm.build_chat_model)


class BaseAgent(ABC):
    spec: ClassVar[AgentSpec]

    def __init__(self, llm_factory: LlmFactory, tools: Sequence[Any]):
        self._llm_factory = llm_factory
        self.tools = list(tools)

    def llm(self, role: str) -> BaseChatModel:
        """역할에 배정된 모델로 LLM을 만든다. 배정되지 않은 역할은 거부한다."""
        if role not in self.spec.models:
            raise KeyError(f"{self.spec.key}에 모델이 배정되지 않은 역할: {role}")
        return self._llm_factory(self.spec.models[role])

    @abstractmethod
    def run(self, request: InvestigationRequest) -> AgentResult: ...
