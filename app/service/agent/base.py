"""에이전트 공통 인터페이스 (실행 계획서 §1.3, ADR-001).

에이전트는 `service/agent/<이름>/v<N>/`에 두고 BaseAgent를 상속해 registry에 등록한다.
LLM·도구·실행 맥락(근거 저장소, 예산, 사용량, 단계 기록)은 생성자로 주입한다.
그래서 테스트는 가짜 LLM으로 같은 코드를 돌리고, 구현이 달라도(v1, v1_loop) 같은 기준으로 잰다.

구현은 _run()만 쓴다. run()이 시간·사용량 집계와 예외 → failed 변환을 공통으로 처리한다.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from traceback import format_exc
from typing import Any, ClassVar, Literal, Protocol

from langchain_core.language_models import BaseChatModel
from pydantic import AwareDatetime, BaseModel

from core.guard.budget import Budget, BudgetTracker
from core.guard.redact import redact_mapping, redact_text
from core.log.logging import get_logging
from service.agent._common.usage import UsageTracker
from service.model import ChatAnswer, ChatRequest, InvestigationRequest, Report
from service.tools.base import EvidenceSink, Tool, ToolContext

AgentStatus = Literal["done", "incomplete", "failed"]  # 완료 / 상한 도달(부분 결과) / 실패


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
    ended_at: AwareDatetime | None = None
    tool: str | None = None
    args: dict[str, Any] = {}
    evidence_ids: list[str] = []
    detail: dict[str, Any] = {}


class StepSink(Protocol):
    """단계 기록 저장 (진행 중 화면 갱신용. 구현: core/db)."""

    def record(self, step: AgentStep) -> None: ...


class MemorySteps:
    def __init__(self):
        self.steps: list[AgentStep] = []

    def record(self, step: AgentStep) -> None:
        self.steps.append(step)


class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, dict[str, int]] = {}
    tool_calls: int = 0
    seconds: float = 0


class AgentResult(BaseModel):
    status: AgentStatus
    report: Report | None = None
    answer: ChatAnswer | None = None  # 이력 질의(history_qa)의 답
    steps: list[AgentStep] = []
    usage: Usage = Usage()
    error: str | None = None  # failed일 때 사유 (마스킹)


@dataclass
class RunContext:
    """조사 실행 하나의 맥락."""

    run_id: str
    namespace: str
    evidence: EvidenceSink
    budget: BudgetTracker
    usage: UsageTracker = field(default_factory=UsageTracker)
    steps: StepSink = field(default_factory=MemorySteps)
    k8s: Any = None  # 네임스페이스에 묶인 조회 클라이언트 (core.k8s)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    http: Any = None  # 네임스페이스에 묶인 HTTP 클라이언트 (core.http)

    def tool_context(self) -> ToolContext:
        return ToolContext(
            self.namespace, self.evidence, self.budget, self.k8s, self.now, http=self.http
        )


LlmFactory = Callable[[str], BaseChatModel]  # 모델 이름 → LLM (core.llm.build_chat_model)


class BaseAgent(ABC):
    spec: ClassVar[AgentSpec]

    def __init__(self, llm_factory: LlmFactory, tools: Sequence[Tool], ctx: RunContext):
        self._llm_factory = llm_factory
        self.tools = list(tools)
        self.ctx = ctx

    def llm(self, role: str) -> BaseChatModel:
        """역할에 배정된 모델로 LLM을 만든다. 배정되지 않은 역할은 거부한다."""
        if role not in self.spec.models:
            raise KeyError(f"{self.spec.key}에 모델이 배정되지 않은 역할: {role}")
        return self._llm_factory(self.spec.models[role])

    @contextmanager
    def step(self, node: str, **detail: Any) -> Iterator[AgentStep]:
        """단계 하나를 기록한다. with 블록 안에서 tool·args·evidence_ids를 채울 수 있다."""
        s = AgentStep(node=node, started_at=self.ctx.now(), detail=detail)
        try:
            yield s
        finally:
            s.ended_at = self.ctx.now()
            s.args, s.detail = redact_mapping(s.args), redact_mapping(s.detail)
            self.ctx.steps.record(s)

    def run(self, request: InvestigationRequest | ChatRequest) -> AgentResult:
        try:
            result = self._run(request)
        except Exception as e:
            # 조사 실패여도 1차 알림은 이미 나갔다. 사유만 남기고, 원인 추적용 traceback은 로그로
            trace = redact_text(format_exc())
            get_logging().error(f"agent={self.spec.key} run_id={self.ctx.run_id} 실패\n{trace}")
            result = AgentResult(status="failed", error=redact_text(f"{type(e).__name__}: {e}"))
        u = self.ctx.usage
        result.usage = Usage(
            llm_calls=u.llm_calls,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            by_model=u.by_model,
            tool_calls=self.ctx.budget.tool_calls,
            seconds=self.ctx.budget.elapsed(),
        )
        if isinstance(self.ctx.steps, MemorySteps):
            result.steps = self.ctx.steps.steps
        return result

    @abstractmethod
    def _run(self, request: InvestigationRequest | ChatRequest) -> AgentResult: ...
