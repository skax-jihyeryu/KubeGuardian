"""Tool → LangChain 도구 변환. 결과는 <tool_output>으로 감싸 데이터임을 표시한다 (상세설계 §6)."""

from langchain_core.tools import StructuredTool

from service.tools.base import Tool, ToolContext


def to_langchain_tool(tool: Tool, ctx: ToolContext) -> StructuredTool:
    def call(**kwargs) -> str:
        return f"<tool_output>{tool.run(kwargs, ctx).model_dump_json()}</tool_output>"

    return StructuredTool.from_function(
        func=call, name=tool.name, description=tool.description, args_schema=tool.Input
    )
