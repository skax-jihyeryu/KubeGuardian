"""history_qa v1: 이력 도구로 질문에 답한다."""

import json
from collections.abc import Sequence
from typing import ClassVar

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from service.agent._common.prompts import load_prompt
from service.agent._common.structured import invoke_structured
from service.agent._common.tool_adapter import wrap_tool_output
from service.agent.base import AgentResult, AgentSpec, BaseAgent, LlmFactory, RunContext
from service.agent.history_qa.v1.config import SPEC
from service.agent.history_qa.v1.tools import HistoryTool
from service.agent.registry import register
from service.model import ChatAnswer, ChatRequest, InvestigationRequest

MAX_ROUNDS = 10
FINAL_ASK = "지금까지 확인한 것으로 ChatAnswer 형식에 맞춰 답하세요."


@register
class HistoryQaV1(BaseAgent):
    spec: ClassVar[AgentSpec] = SPEC

    def __init__(self, llm_factory: LlmFactory, tools: Sequence[HistoryTool], ctx: RunContext):
        super().__init__(llm_factory, tools, ctx)

    def _run(self, request: InvestigationRequest | ChatRequest) -> AgentResult:
        assert isinstance(request, ChatRequest)
        by_name = {t.name: t for t in self.tools}
        schemas = [
            StructuredTool.from_function(
                func=lambda **_: "", name=t.name, description=t.description, args_schema=t.Input
            )
            for t in self.tools
        ]
        llm = self.llm("answer")
        messages: list = [SystemMessage(load_prompt(self.spec.prompts_dir, "system"))]
        for turn in request.history:
            messages.append(
                HumanMessage(turn.text) if turn.role == "user" else AIMessage(turn.text)
            )
        messages.append(HumanMessage(request.question))

        limited = True
        for _ in range(MAX_ROUNDS):
            reply = llm.bind_tools(schemas).invoke(messages)
            self.ctx.usage.add(reply)
            messages.append(reply)
            if not reply.tool_calls:
                limited = False
                break
            for call in reply.tool_calls:
                result = self._call_tool(by_name.get(call["name"]), call["args"], call["name"])
                text = wrap_tool_output(json.dumps(result, ensure_ascii=False, default=str))
                messages.append(ToolMessage(text, tool_call_id=call["id"]))
            if not self.ctx.budget.can_call_tool():
                break

        answer = invoke_structured(
            llm, ChatAnswer, [*messages, ("user", FINAL_ASK)], usage=self.ctx.usage
        )
        if limited:
            answer.partial = True
        return AgentResult(status="incomplete" if limited else "done", answer=answer)

    def _call_tool(self, tool: HistoryTool | None, args: dict, name: str) -> dict:
        with self.step("tool") as s:
            s.tool, s.args = name, args
            if tool is None:
                return {"ok": False, "error": "UNKNOWN_TOOL"}
            return tool.run(args, self.ctx.budget)
