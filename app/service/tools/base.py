"""에이전트 도구 인터페이스 (상세설계 §3.1, 실행 계획서 §1.3, ADR-001).

도구는 LangGraph에 묶이지 않는 일반 클래스다.
직접 구현 루프(v1_loop)와 MCP 서버도 같은 도구를 쓴다.
run()이 공통 규칙을 강제한다:
입력 검증 → 호출 상한 → 수집 → 대상 범위 확인 → 마스킹 → 근거 저장 → 요약 반환.
수집 실패도 근거로 저장하고 ok=False로 돌려준다.

대상 네임스페이스는 도구 입력으로 받지 않고 ToolContext가 정한다.
도구는 ctx.k8s(네임스페이스에 묶인 조회 클라이언트)만 써서 범위 밖 호출 자체를 하지 않는다.
run()의 범위 확인은 이중 안전장치다.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.budget import BudgetTracker
from core.guard.redact import redact_mapping, redact_text
from service.model import Evidence, EvidenceKind

_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_NAMESPACE_WORDS = {"ns", "namespace", "namespaces"}
_SUBJECT = re.compile(r"[^/\s]+/[^/\s]+/[^/\s]+")  # <namespace>/<Kind>/<name>


class EvidenceSink(Protocol):
    """근거 저장소 (구현: core/db의 EvidenceStore)."""

    def add(self, evidence: Evidence) -> str:
        """저장하고 발급한 id(ev-0001)를 돌려준다."""
        ...


@dataclass(frozen=True)
class ToolContext:
    namespace: str  # 진단 대상 네임스페이스
    evidence: EvidenceSink
    budget: BudgetTracker | None = None  # 도구 호출 상한·집계
    k8s: Any = None  # 네임스페이스에 묶인 조회 전용 클라이언트 (core.k8s)
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

    ok: bool = True
    evidence_id: str | None  # 상한 도달로 호출하지 않았으면 None
    summary: str
    data: dict[str, Any]
    truncated: bool = False


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]  # 언제 쓰는지 한 줄 (도구 설명이 곧 프롬프트)
    Input: ClassVar[type[BaseModel]]
    failure_kind: ClassVar[EvidenceKind] = "resource"  # 수집 실패를 저장할 근거 종류

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "Input" not in cls.__dict__:
            return
        bad = _namespace_fields(cls.Input)
        if bad:
            raise TypeError(f"{cls.__name__}: 도구 입력에 네임스페이스 필드를 두지 않는다 {bad}")
        if cls.Input.model_config.get("extra") != "forbid":
            # 모르는 인자(namespace 등)를 조용히 버리지 않고 거부한다
            strict = ConfigDict(extra="forbid")
            cls.Input = type(cls.Input.__name__, (cls.Input,), {"model_config": strict})

    @abstractmethod
    def collect(self, args: Any, ctx: ToolContext) -> ToolObservation: ...

    def run(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            args = self.Input.model_validate(raw_args)
        except ValidationError as e:
            fields = [".".join(map(str, err["loc"])) for err in e.errors()]
            raise ServiceException(
                ErrorCode.VALIDATION_ERROR, detail=f"{self.name}: {fields}"
            ) from e

        if ctx.budget is not None:
            if (
                not ctx.budget.can_call_tool()
            ):  # 상한은 구현이 아니라 여기서 강제한다 (v1·v1_loop 같은 기준)
                return ToolResult(
                    ok=False,
                    evidence_id=None,
                    summary="도구 호출 상한 도달. 더 호출하지 말고 지금까지의 근거로 판단하세요",
                    data={},
                )
            ctx.budget.count_tool_call()

        try:
            obs = self.collect(args, ctx)
        except Exception as e:  # 호출 실패도 근거로 남기고 조사는 계속한다 (상세설계 §3.1)
            if isinstance(e, ServiceException) and e.error_code is ErrorCode.TOOL_SCOPE_VIOLATION:
                raise
            detail = e.detail if isinstance(e, ServiceException) else str(e)
            message = redact_text(f"{type(e).__name__}: {detail}")
            subject = f"{ctx.namespace}/Tool/{self.name}"
            evidence_id = self._save(ctx, self.failure_kind, subject, self.name, {"error": message})
            return ToolResult(
                ok=False,
                evidence_id=evidence_id,
                summary=f"조회 실패: {message}",
                data={"error": message},
            )

        subject = obs.subject
        in_scope = subject.startswith(f"{ctx.namespace}/") or subject.startswith("cluster/Node/")
        if not ctx.namespace or not in_scope or not _SUBJECT.fullmatch(subject):
            raise ServiceException(
                ErrorCode.TOOL_SCOPE_VIOLATION, detail=f"{self.name}: {redact_text(subject)}"
            )

        data, summary = redact_mapping(obs.data), redact_text(obs.summary)
        evidence_id = self._save(ctx, obs.kind, redact_text(subject), redact_text(obs.source), data)
        return ToolResult(
            evidence_id=evidence_id, summary=summary, data=data, truncated=obs.truncated
        )

    @staticmethod
    def _save(ctx: ToolContext, kind: EvidenceKind, subject: str, source: str, data: dict) -> str:
        return ctx.evidence.add(
            Evidence(kind=kind, subject=subject, collected_at=ctx.now(), source=source, data=data)
        )


def _namespace_fields(model: type[BaseModel], prefix: str = "") -> list[str]:
    """필드 이름·alias를 단어로 나눠(snake·camelCase) 네임스페이스 필드를 찾는다.

    중첩 모델도 본다.
    """
    found = []
    for name, info in model.model_fields.items():
        for label in filter(None, (name, info.alias)):
            words = {w.lower() for part in re.split(r"[_\-.]", label) for w in _WORD.findall(part)}
            if words & _NAMESPACE_WORDS:
                found.append(prefix + label)
        ann = info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            found += _namespace_fields(ann, f"{prefix}{name}.")
    return found
