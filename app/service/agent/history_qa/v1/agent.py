"""history_qa v1: 이력 도구로 질문에 답한다."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
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
FINAL_ASK = "지금까지 확인한 것으로 ChatAnswer 형식의 JSON 하나로 답하세요. 스키마: " + json.dumps(
    ChatAnswer.model_json_schema(), ensure_ascii=False
)


@dataclass
class Seen:
    """이번 실행에서 ok 도구 결과에 나온 번호. 답변 인용은 여기 있는 것만 인정한다."""

    evidence: set[str] = field(default_factory=set)
    issues: set[int] = field(default_factory=set)
    runs: set[int] = field(default_factory=set)

    def collect(self, name: str, result: dict) -> None:
        if not result.get("ok"):
            return
        match name:
            case "list_runs":
                for r in result.get("runs", []):
                    self.runs.add(r["id"])
                    self.issues.update(i["id"] for i in r.get("incidents", []))
            case "get_snapshot":
                self.runs.add(result["run_id"])
            case "get_evidence":
                if "evidences" in result:
                    self.evidence.update(e["id"] for e in result["evidences"])
                    self.runs.add(result["run_id"])
                else:
                    self.evidence.add(result["id"])
            case "list_issues":
                self._add_as_of(result)
                self.issues.update(i["id"] for i in result.get("issues", []))
            case "list_alerts":
                self._add_as_of(result)
                self.issues.update(a["issue_id"] for a in result.get("alerts", []))

    def _add_as_of(self, result: dict) -> None:
        if result.get("as_of_run") is not None:
            self.runs.add(result["as_of_run"])


@register
class HistoryQaV1(BaseAgent):
    spec: ClassVar[AgentSpec] = SPEC

    def __init__(self, llm_factory: LlmFactory, tools: Sequence[HistoryTool], ctx: RunContext):
        super().__init__(llm_factory, tools, ctx)
        self.seen = Seen()

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
        answer.partial = limited
        return AgentResult(status="incomplete" if limited else "done", answer=answer)

    def _call_tool(self, tool: HistoryTool | None, args: dict, name: str) -> dict:
        with self.step("tool") as s:
            s.tool, s.args = name, args
            if tool is None:
                return {"ok": False, "error": "UNKNOWN_TOOL"}
            result = tool.run(args, self.ctx.budget)
            self.seen.collect(name, result)
            return result
