"""investigator v1 LangGraph 그래프 (상세설계 §2.2).

load_context → preliminary → triage → act ⇄ tools → write_report → validate
- 분기·상한은 LLM이 아니라 코드가 정한다 (예산은 Tool.run이 센다).
- 1순위 문제 1건만 조사한다. 가설 루프·여러 문제 순회는 #21.
- LLM 오류는 BaseAgent.run이 failed로 바꾼다.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from core.guard.validate import validate_report
from service.agent._common.prompts import load_prompt
from service.agent._common.structured import invoke_structured
from service.agent._common.tool_adapter import to_langchain_tool, wrap_tool_output
from service.agent.investigator.v1.context import load_knowledge, render_board, render_facts
from service.agent.investigator.v1.state import InvestigationState, TriageResult
from service.model import Report
from service.model.report import Fact

if TYPE_CHECKING:
    from service.agent.investigator.v1.agent import InvestigatorV1

KNOWLEDGE_DIR = Path(__file__).parents[3] / "knowledge"
EVENTS_MINUTES = 30
_RETRY_NOTE = (
    "결론에 실제 근거 번호(ev-N)가 없습니다. 도구 결과에 있는 근거 번호를 인용해 다시 쓰세요."
)


def build_graph(agent: "InvestigatorV1"):
    ctx = agent.ctx
    spec = agent.spec
    tool_ctx = ctx.tool_context()
    tools = {t.name: t for t in agent.tools}
    lc_tools = {name: to_langchain_tool(t, tool_ctx) for name, t in tools.items()}

    def prompt(name: str, **values: str) -> str:
        return load_prompt(spec.prompts_dir, name, **values)

    def system() -> SystemMessage:
        b = spec.budget
        return SystemMessage(
            prompt(
                "system",
                namespace=ctx.namespace,
                per_issue_budget=str(b.per_issue_tool_calls),
                total_budget=str(b.total_tool_calls),
                knowledge=load_knowledge(KNOWLEDGE_DIR),
            )
        )

    def call_tool(name: str, args: dict) -> Fact | None:
        if name not in tools:
            return None
        result = tools[name].run(args, tool_ctx)
        if not result.evidence_id:
            return None
        return Fact(statement=f"{name}: {result.summary}", evidence_ids=[result.evidence_id])

    def load_context(state: InvestigationState) -> InvestigationState:
        board = state["request"].signal_board
        seen = [s.evidence_id for ss in board.by_service.values() for s in ss if s.evidence_id]
        return {"seen": seen, "limited": False, "report_attempts": 0, "status": "running"}

    def preliminary(state: InvestigationState) -> InvestigationState:
        """가설 없이 확정할 수 있는 사실 (LLM 없음): 서비스 구성, 최근 이벤트, 경로 B면 시나리오."""
        with agent.step("preliminary") as s:
            calls = [("get_topology", {}), ("get_events", {"since_minutes": EVENTS_MINUTES})]
            scenario = tools.get("run_api_scenario")
            if state["request"].gate_path == "B" and scenario is not None:
                calls += [("run_api_scenario", {"name": n}) for n in scenario.scenario_names[:1]]
            facts = [f for name, args in calls if (f := call_tool(name, args))]
            s.evidence_ids = [i for f in facts for i in f.evidence_ids]
        return {"facts": facts, "seen": state["seen"] + s.evidence_ids}

    def triage(state: InvestigationState) -> InvestigationState:
        req = state["request"]
        with agent.step("triage") as s:
            text = prompt(
                "triage",
                gate_path=req.gate_path,
                board=render_board(req.signal_board),
                facts=render_facts(state["facts"]),
                symptom=req.user_symptom or "(없음)",
            )
            result = invoke_structured(
                agent.llm("triage"), TriageResult, [system(), HumanMessage(text)], usage=ctx.usage
            )
            s.detail = {"issues": [i.title for i in result.issues], "runbook": result.runbook}
        ctx.budget.start_issue()
        issue = result.issues[0].title if result.issues else "(선별된 문제 없음)"
        first = HumanMessage(
            prompt(
                "investigate",
                issue=issue,
                runbook=result.runbook or "(없음)",
                facts=render_facts(state["facts"]),
            )
        )
        return {"triage": result, "messages": [system(), first]}

    def act(state: InvestigationState) -> InvestigationState:
        with agent.step("act") as s:
            reply = (
                agent.llm("investigate")
                .bind_tools(list(lc_tools.values()))
                .invoke(state["messages"])
            )
            ctx.usage.add(reply)
            s.detail = {"tool_calls": [c["name"] for c in reply.tool_calls]}
        return {"messages": state["messages"] + [reply]}

    def after_act(state: InvestigationState) -> str:
        if not state["messages"][-1].tool_calls:
            return "write_report"
        if not ctx.budget.can_call_tool():
            return "limit"
        return "tools"

    def limit(state: InvestigationState) -> InvestigationState:
        # 상한에 닿았으면 남은 도구 호출 요청은 실행하지 않고 보고서로 간다
        last = state["messages"][-1]
        notes = [
            ToolMessage(
                wrap_tool_output(json.dumps({"ok": False, "error": "도구 호출 상한 도달"})),
                tool_call_id=c["id"],
            )
            for c in last.tool_calls
        ]
        return {"messages": state["messages"] + notes, "limited": True}

    def run_tools(state: InvestigationState) -> InvestigationState:
        messages = list(state["messages"])
        seen = list(state["seen"])
        limited = state["limited"]
        for call in messages[-1].tool_calls:
            if not ctx.budget.can_call_tool():
                limited = True  # 한 응답의 병렬 호출 중 상한에 걸린 것은 Tool.run이 거절한다
            with agent.step("tools") as s:
                s.tool, s.args = call["name"], dict(call["args"])
                lc = lc_tools.get(call["name"])
                if lc is None:
                    body = {"ok": False, "error": f"없는 도구: {call['name']}"}
                    content = wrap_tool_output(json.dumps(body, ensure_ascii=False))
                else:
                    content = lc.invoke(call["args"])
                    ev = _evidence_id(content)
                    if ev:
                        s.evidence_ids = [ev]
                        seen.append(ev)
                messages.append(ToolMessage(content, tool_call_id=call["id"]))
        return {"messages": messages, "seen": seen, "limited": limited}

    def write_report(state: InvestigationState) -> InvestigationState:
        attempts = state["report_attempts"] + 1
        with agent.step("write_report", attempt=attempts):
            issue = state["triage"].issues[0].title if state["triage"].issues else "(없음)"
            history = list(state["messages"])
            if attempts > 1:
                history.append(HumanMessage(_RETRY_NOTE))
            history.append(
                HumanMessage(prompt("report", issue=issue, facts=render_facts(state["facts"])))
            )
            report = invoke_structured(agent.llm("report"), Report, history, usage=ctx.usage)
        return {"report": report, "report_attempts": attempts}

    def validate(state: InvestigationState) -> InvestigationState:
        seen = set(state["seen"])
        with agent.step("validate") as s:
            out = validate_report(state["report"], seen.__contains__)
            s.detail = {"demoted": out.demoted, "conclusion_supported": out.conclusion_supported}
        report = out.report.model_copy(update={"conclusion_supported": out.conclusion_supported})
        if not out.conclusion_supported and state["report_attempts"] >= 2:
            report = report.model_copy(update={"confidence": "low"})
        if not report.preliminary_facts:
            report = report.model_copy(update={"preliminary_facts": state["facts"]})
        limited = state["limited"] or ctx.budget.time_over()
        return {"report": report, "status": "incomplete" if limited else "done"}

    def after_validate(state: InvestigationState) -> str:
        unsupported = not state["report"].evidence_ids
        return "write_report" if unsupported and state["report_attempts"] < 2 else END

    def after_tools(state: InvestigationState) -> str:
        return "limit_time" if ctx.budget.time_over() else "act"

    def limit_time(state: InvestigationState) -> InvestigationState:
        return {"limited": True}

    g = StateGraph(InvestigationState)
    for name, fn in [
        ("load_context", load_context),
        ("preliminary", preliminary),
        ("triage", triage),
        ("act", act),
        ("tools", run_tools),
        ("limit", limit),
        ("limit_time", limit_time),
        ("write_report", write_report),
        ("validate", validate),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "preliminary")
    g.add_edge("preliminary", "triage")
    g.add_edge("triage", "act")
    g.add_conditional_edges("act", after_act, ["tools", "limit", "write_report"])
    g.add_conditional_edges("tools", after_tools, ["act", "limit_time"])
    g.add_edge("limit", "write_report")
    g.add_edge("limit_time", "write_report")
    g.add_edge("write_report", "validate")
    g.add_conditional_edges("validate", after_validate, ["write_report", END])
    return g.compile()


def _evidence_id(content: str) -> str | None:
    """어댑터가 감싼 도구 결과에서 evidence_id를 꺼낸다."""
    inner = content.removeprefix("<tool_output>").removesuffix("</tool_output>")
    try:
        return json.loads(inner).get("evidence_id")
    except (json.JSONDecodeError, AttributeError):
        return None
