"""Tool → LangChain 도구 변환 (상세설계 §6 프롬프트 인젝션 방어).

결과는 <tool_output>으로 감싸 데이터임을 표시한다. 결과 안의 < > 는 JSON 유니코드 이스케이프로
바꿔, 로그·이벤트에 든 "</tool_output>" 같은 문자열이 감싸기를 닫지 못하게 한다.
도구 오류(입력 검증·범위 위반)도 같은 형식으로 LLM에 돌려준다.
"""

import json

from langchain_core.tools import StructuredTool, ToolException
from pydantic import ValidationError

from core.error.service_exception import ServiceException
from core.guard.redact import redact_text
from service.tools.base import Tool, ToolContext


def wrap_tool_output(json_text: str) -> str:
    return f"<tool_output>{json_text.replace('<', '\\u003c').replace('>', '\\u003e')}</tool_output>"


def to_langchain_tool(tool: Tool, ctx: ToolContext) -> StructuredTool:
    def call(**kwargs) -> str:
        try:
            return wrap_tool_output(tool.run(kwargs, ctx).model_dump_json())
        except ServiceException as e:
            raise ToolException(e.error_code.name) from e

    def on_error(e: ToolException) -> str:
        return wrap_tool_output(json.dumps({"ok": False, "error": redact_text(str(e))}))

    def on_validation_error(e: ValidationError) -> str:
        # 어느 인자가 틀렸는지만 알려 준다 (값은 넣지 않는다)
        fields = [".".join(map(str, err["loc"])) for err in e.errors()]
        body = {"ok": False, "error": "VALIDATION_ERROR", "fields": fields}
        return wrap_tool_output(json.dumps(body, ensure_ascii=False))

    return StructuredTool.from_function(
        func=call,
        name=tool.name,
        description=tool.description,
        args_schema=tool.Input,
        handle_tool_error=on_error,
        handle_validation_error=on_validation_error,
    )
