"""구조화 출력 파싱 방어 (상세설계 §5 Output Parsing).

코드펜스·앞뒤 설명문·빈 응답을 견디고, 스키마 검증에 실패하면 오류를 알려 주고 1회 재시도한다.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from core.error.service_exception import ErrorCode, ServiceException

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_structured[T: BaseModel](text: str, schema: type[T]) -> T:
    """응답 텍스트에서 JSON 객체를 찾아 schema로 검증한다. 실패하면 ValueError."""
    if m := _FENCE.search(text):
        text = m[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON 객체가 없습니다")
    try:
        return schema.model_validate(json.loads(text[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(str(e)) from e


def invoke_structured[T: BaseModel](
    llm: BaseChatModel, schema: type[T], messages: Sequence[Any], retries: int = 1
) -> T:
    history = list(messages)
    error = ""
    for _ in range(retries + 1):
        reply = llm.invoke(history)
        try:
            return parse_structured(str(reply.content), schema)
        except ValueError as e:
            error = str(e)
            history += [
                reply,
                ("user", f"응답을 {schema.__name__} 형식의 JSON 하나로 다시 주세요. 오류: {error}"),
            ]
    raise ServiceException(ErrorCode.LLM_OUTPUT_INVALID, detail=error)
