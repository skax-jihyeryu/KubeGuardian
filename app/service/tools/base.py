"""에이전트 도구 인터페이스 (상세설계 §3.1, 실행 계획서 §1.3).

도구는 LangGraph에 묶이지 않는 일반 클래스다.
직접 구현 루프(v1_loop)와 MCP 서버도 같은 도구를 쓴다.
run()이 공통 규칙을 강제한다:
입력 검증 → 대상 네임스페이스 고정 → 수집 → 마스킹 → 근거 저장 → 요약 반환.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ValidationError

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import redact_mapping, redact_text
from service.model import Evidence, EvidenceKind


class EvidenceSink(Protocol):
    """근거 저장소 (구현: core/db의 EvidenceStore)."""

    def add(self, evidence: Evidence) -> str:
        """저장하고 발급한 id(ev-0001)를 돌려준다."""
        ...


@dataclass(frozen=True)
class ToolContext:
    namespace: str  # 진단 대상 네임스페이스. 도구는 이 밖을 조회하지 않는다
    evidence: EvidenceSink
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class ToolObservation(BaseModel):
    """도구 구현이 돌려주는 원본 결과 (마스킹 전)."""

    kind: EvidenceKind
    subject: str  # <namespace>/<Kind>/<name>, 노드는 cluster/Node/<name>
    source: str
    summary: str  # LLM에 줄 한 줄 요약
    data: dict[str, Any]  # 필드 선별·길이 제한을 거친 결과
    truncated: bool = False


class ToolResult(BaseModel):
    """LLM에 돌려주는 공통 출력 형식."""

    evidence_id: str
    summary: str
    data: dict[str, Any]
    truncated: bool = False


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]  # 언제 쓰는지 한 줄 (도구 설명이 곧 프롬프트)
    Input: ClassVar[type[BaseModel]]

    @abstractmethod
    def collect(self, args: Any, ctx: ToolContext) -> ToolObservation: ...

    def run(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            args = self.Input.model_validate(raw_args)
        except ValidationError as e:
            raise ServiceException(ErrorCode.VALIDATION_ERROR, detail=f"{self.name}: {e}") from e
        if getattr(args, "namespace", ctx.namespace) != ctx.namespace:
            raise ServiceException(ErrorCode.TOOL_SCOPE_VIOLATION, detail=f"{self.name}: {args}")

        obs = self.collect(args, ctx)
        if obs.subject.split("/", 1)[0] not in (ctx.namespace, "cluster"):
            raise ServiceException(ErrorCode.TOOL_SCOPE_VIOLATION, detail=obs.subject)

        data, summary = redact_mapping(obs.data), redact_text(obs.summary)
        evidence_id = ctx.evidence.add(
            Evidence(
                kind=obs.kind,
                subject=obs.subject,
                collected_at=ctx.now(),
                source=obs.source,
                data=data,
            )
        )
        return ToolResult(
            evidence_id=evidence_id, summary=summary, data=data, truncated=obs.truncated
        )
