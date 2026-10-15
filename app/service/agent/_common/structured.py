"""구조화 출력 (상세설계 §5 Output Parsing).

제공사가 지원하면 with_structured_output을 쓰고, 지원하지 않거나 형식이 깨지면 텍스트에서 JSON을
찾는다. 코드펜스·앞뒤 설명문·리스트 content를 견디고, 실패하면 오류를 알려 주고 1회 재시도한다.
LLM 호출 실패는 LLM_CALL_FAILED, 형식 실패는 LLM_OUTPUT_INVALID로 정규화한다.
"""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import redact_text
from service.agent._common.usage import UsageTracker

_DECODER = json.JSONDecoder(strict=False)  # 문자열 안 줄바꿈 허용


def parse_structured[T: BaseModel](text: str, schema: type[T]) -> T:
    """텍스트에서 schema에 맞는 첫 JSON 객체를 찾는다. 없으면 ValueError."""
    last_error = "JSON 객체가 없습니다"
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        try:
            obj, _ = _DECODER.raw_decode(text, start)
            return schema.model_validate(obj)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)
    raise ValueError(last_error)


def invoke_structured[T: BaseModel](
    llm: BaseChatModel,
    schema: type[T],
    messages: Sequence[Any],
    retries: int = 1,
    usage: UsageTracker | None = None,
) -> T:
    native = _invoke_native(llm, schema, messages, usage)
    if native is not None:
        return native

    history = list(messages)
    error = ""
    for _ in range(retries + 1):
        reply = _invoke(llm, history, usage)
        try:
            return parse_structured(reply.text, schema)
        except ValueError as e:
            error = str(e)
            history += [
                AIMessage(content=reply.text),  # tool_calls는 빼고 본문만 남긴다
                ("user", f"응답을 {schema.__name__} 형식의 JSON 하나로 다시 주세요. 오류: {error}"),
            ]
    raise ServiceException(ErrorCode.LLM_OUTPUT_INVALID, detail=redact_text(error))


def _invoke_native[T: BaseModel](
    llm: BaseChatModel, schema: type[T], messages: Sequence[Any], usage: UsageTracker | None
) -> T | None:
    """제공사의 구조화 출력. 지원하지 않거나 형식이 깨지면 None (텍스트 파싱으로 넘어간다)."""
    try:
        structured = llm.with_structured_output(schema, include_raw=True)
    except NotImplementedError:
        return None
    try:
        out = structured.invoke(list(messages))
    except Exception as e:
        raise ServiceException(ErrorCode.LLM_CALL_FAILED, detail=redact_text(str(e))) from e
    if usage is not None and isinstance(out.get("raw"), AIMessage):
        usage.add(out["raw"])
    parsed = out.get("parsed")
    return parsed if isinstance(parsed, schema) else None


def _invoke(llm: BaseChatModel, history: list, usage: UsageTracker | None) -> AIMessage:
    try:
        reply = llm.invoke(history)
    except Exception as e:
        raise ServiceException(ErrorCode.LLM_CALL_FAILED, detail=redact_text(str(e))) from e
    if usage is not None:
        usage.add(reply)
    return reply
