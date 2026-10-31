"""investigator v1 LangGraph 그래프 (상세설계 §2.2).

load_context → preliminary → triage → plan → act ⇄ tools → write_report → validate
  (act가 끝나면 next_issue → plan으로 다음 문제를 조사할 수 있다)
  validate 뒤 stop_check가 빈틈을 찾으면 1회만 act로 되돌려 재조사한다.
- 문제당 상한(8)만 차면 조사를 끝낸 것으로 보고 다음 문제나 보고서로 간다 (skip_calls).
  전체 상한(20)·시간(150초)에 닿았을 때만 limited(incomplete)다. 재조사 중 상한은 limited가 아니다.
- 분기·상한은 LLM이 아니라 코드가 정한다 (예산은 Tool.run이 센다).
- plan이 문제마다 가설과 확인할 영역을 세운다. act가 도구를 더 부르지 않으면 예산이 남을 때
  next_issue가 2·3순위 문제로 넘어간다. 도구마다 판정하는 노드는 없다 (판정은 보고서에서).
- LLM 오류는 BaseAgent.run이 failed로 바꾼다.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from core.guard.judgement import SIGNAL_SOURCE, SOURCE_OF_TOOL, SYMPTOM_SOURCE, judge
from core.guard.validate import validate_report
from service.agent._common.prompts import load_prompt
from service.agent._common.structured import invoke_structured
from service.agent._common.tool_adapter import to_langchain_tool, wrap_tool_output
from service.agent.investigator.v1.context import (
    load_knowledge,
    render_board,
    render_facts,
    render_plan,
    render_symptoms,
)
from service.agent.investigator.v1.coverage import (
    cause_fetched,
    downgrade_unresolved_cause,
    find_gaps,
    mark_unobserved,
)
from service.agent.investigator.v1.state import (
    InvestigationPlan,
    InvestigationState,
    Observation,
    TriageResult,
)
from service.model import Report
from service.model.report import Fact

if TYPE_CHECKING:
    from service.agent.investigator.v1.agent import InvestigatorV1

KNOWLEDGE_DIR = Path(__file__).parents[3] / "knowledge"
EVENTS_MINUTES = 30
NEXT_ISSUE_MIN_SECONDS = 40  # 남은 시간이 이보다 적으면 다음 문제를 조사하지 않는다
_NOT_INVESTIGATED = "(예산 부족으로 조사하지 못함)"
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

    def observation(evidence_id: str, name: str, args: dict, ok: bool = True) -> Observation:
        if name == "get_resource":
            return Observation(
                evidence_id=evidence_id,
                tool=name,
                kind=args.get("kind"),
                name=args.get("name"),
                ok=ok,
            )
        return Observation(evidence_id=evidence_id, tool=name, ok=ok)

    def call_tool(name: str, args: dict) -> tuple[Fact, bool] | None:
        if name not in tools:
            return None
        result = tools[name].run(args, tool_ctx)
        if not result.evidence_id:
            return None
        fact = Fact(statement=f"{name}: {result.summary}", evidence_ids=[result.evidence_id])
        return fact, result.ok

    def issue_title(state: InvestigationState, index: int) -> str:
        issues = state["triage"].issues
        return issues[index].title if index < len(issues) else "(선별된 문제 없음)"

    def load_context(state: InvestigationState) -> InvestigationState:
        board = state["request"].signal_board
        seen = [s.evidence_id for ss in board.by_service.values() for s in ss if s.evidence_id]
        seen += [x.evidence_id for x in state["request"].symptoms if x.evidence_id]
        return {
            "seen": seen,
            "limited": False,
            "report_attempts": 0,
            "status": "running",
            "current_issue": 0,
            "plans": [],
            "observations": [],
            "rechecked": False,
            "recheck": False,
        }

    def preliminary(state: InvestigationState) -> InvestigationState:
        """가설 없이 확정할 수 있는 사실 (LLM 없음): 서비스 구성, 최근 이벤트, 경로 B면 시나리오."""
        with agent.step("preliminary") as s:
            calls = [("get_topology", {}), ("get_events", {"since_minutes": EVENTS_MINUTES})]
            scenario = tools.get("run_api_scenario")
            if state["request"].gate_path == "B" and scenario is not None:
                # 점검에서 실패한 시나리오를 먼저 다시 돌린다. 없으면(사용자 증상만) 첫 시나리오
                failed = [x.scenario for x in state["request"].symptoms]
                calls += [
                    ("run_api_scenario", {"name": n})
                    for n in (failed or scenario.scenario_names)[:1]
                ]
            facts, obs = [], []
            for name, args in calls:
                if called := call_tool(name, args):
                    fact, ok = called
                    facts.append(fact)
                    obs.append(observation(fact.evidence_ids[0], name, args, ok))
            s.evidence_ids = [i for f in facts for i in f.evidence_ids]
        return {
            "facts": facts,
            "seen": state["seen"] + s.evidence_ids,
            "observations": state["observations"] + obs,
        }

    def triage(state: InvestigationState) -> InvestigationState:
        req = state["request"]
        with agent.step("triage") as s:
            text = prompt(
                "triage",
                gate_path=req.gate_path,
                board=render_board(req.signal_board),
                facts=render_facts(state["facts"]),
                symptoms=render_symptoms(req.symptoms),
                symptom=req.user_symptom or "(없음)",
            )
            result = invoke_structured(
                agent.llm("triage"), TriageResult, [system(), HumanMessage(text)], usage=ctx.usage
            )
            s.detail = {"issues": [i.title for i in result.issues], "runbook": result.runbook}
        return {"triage": result}

    def plan(state: InvestigationState) -> InvestigationState:
        """문제 하나의 가설 계획(LLM)을 세우고 조사 메시지를 덧붙인다. 문제가 없으면 빈 계획."""
        index = state["current_issue"]
        triage_result = state["triage"]
        issue = issue_title(state, index)
        runbook = triage_result.runbook or "(없음)"
        with agent.step("plan") as s:
            if triage_result.issues:
                text = prompt(
                    "plan", issue=issue, runbook=runbook, facts=render_facts(state["facts"])
                )
                made = invoke_structured(
                    agent.llm("plan"),
                    InvestigationPlan,
                    [system(), HumanMessage(text)],
                    usage=ctx.usage,
                )
            else:
                made = InvestigationPlan()
            s.detail = {"issue": issue, "hypotheses": [h.statement for h in made.hypotheses]}
        ctx.budget.start_issue()
        message = HumanMessage(
            prompt(
                "investigate",
                issue=issue,
                runbook=runbook,
                facts=render_facts(state["facts"]),
                plan=render_plan(made),
            )
        )
        history = state["messages"] if index else [system()]
        return {"plans": state["plans"] + [made], "messages": [*history, message]}

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

    def has_next_issue(state: InvestigationState) -> bool:
        """재조사 중이 아니고, 다음 문제가 있고, 호출 예산과 시간이 남아 있다."""
        b = ctx.budget
        return (
            not state["rechecked"]
            and state["current_issue"] + 1 < len(state["triage"].issues)
            and b.total_left()
            and b.remaining_seconds() >= NEXT_ISSUE_MIN_SECONDS
        )

    def next_issue(state: InvestigationState) -> InvestigationState:
        return {"current_issue": state["current_issue"] + 1}

    def after_act(state: InvestigationState) -> str:
        if not state["messages"][-1].tool_calls:
            return "next_issue" if has_next_issue(state) else "write_report"
        if ctx.budget.can_call_tool():
            return "tools"
        # 재조사 중이거나 문제당 상한만 찬 경우는 조사가 끝난 것이지 중단이 아니다
        if state["rechecked"] or ctx.budget.total_left():
            return "skip_calls"
        return "limit"

    def answer_skipped(state: InvestigationState) -> list:
        """실행하지 않는 도구 호출 요청에 상한 도달을 답해 대화를 맞춘다."""
        return [
            ToolMessage(
                wrap_tool_output(
                    json.dumps({"ok": False, "error": "도구 호출 상한 도달"}, ensure_ascii=False)
                ),
                tool_call_id=c["id"],
            )
            for c in state["messages"][-1].tool_calls
        ]

    def limit(state: InvestigationState) -> InvestigationState:
        # 전체 상한·시간에 닿았으면 남은 도구 호출 요청은 실행하지 않고 보고서로 간다
        return {"messages": state["messages"] + answer_skipped(state), "limited": True}

    def skip_calls(state: InvestigationState) -> InvestigationState:
        return {"messages": state["messages"] + answer_skipped(state)}

    def after_skip_calls(state: InvestigationState) -> str:
        return "next_issue" if has_next_issue(state) else "write_report"

    def run_tools(state: InvestigationState) -> InvestigationState:
        messages = list(state["messages"])
        seen = list(state["seen"])
        obs = list(state["observations"])
        limited = state["limited"]
        for call in messages[-1].tool_calls:
            if not ctx.budget.total_left() and not state["rechecked"]:
                limited = True  # 한 응답의 병렬 호출 중 상한에 걸린 것은 Tool.run이 거절한다
            with agent.step("tools") as s:
                s.tool, s.args = call["name"], dict(call["args"])
                lc = lc_tools.get(call["name"])
                if lc is None:
                    body = {"ok": False, "error": f"없는 도구: {call['name']}"}
                    content = wrap_tool_output(json.dumps(body, ensure_ascii=False))
                else:
                    content = lc.invoke(call["args"])
                    ev, ok = _parse_result(content)
                    if ev:
                        s.evidence_ids = [ev]
                        seen.append(ev)
                        obs.append(observation(ev, call["name"], dict(call["args"]), ok))
                messages.append(ToolMessage(content, tool_call_id=call["id"]))
        return {"messages": messages, "seen": seen, "observations": obs, "limited": limited}

    def write_report(state: InvestigationState) -> InvestigationState:
        attempts = state["report_attempts"] + 1
        with agent.step("write_report", attempt=attempts):
            issues = state["triage"].issues
            done = state["current_issue"] + 1
            issue = (
                "\n".join(
                    f"{n}. {i.title}" + ("" if n <= done else f" {_NOT_INVESTIGATED}")
                    for n, i in enumerate(issues, 1)
                )
                or "(없음)"
            )
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
        # 재조사 중 시간이 넘은 것은 이미 끝난 조사를 미완료로 바꾸지 않는다
        limited = state["limited"] or (not state["rechecked"] and ctx.budget.time_over())
        return {"report": report, "status": "incomplete" if limited else "done"}

    def after_validate(state: InvestigationState) -> str:
        unsupported = not state["report"].evidence_ids
        return "write_report" if unsupported and state["report_attempts"] < 2 else "stop_check"

    def stop_check(state: InvestigationState) -> InvestigationState:
        """보고서 뒤 코드 검사: 빈틈이 있으면 1회만 재조사하고, 아니면 못 본 영역을 내려 끝낸다."""
        report, obs = state["report"], state["observations"]
        with agent.step("stop_check") as s:
            gaps = find_gaps(report, obs)
            recheck = (
                bool(gaps)
                and not state["rechecked"]
                and not state["limited"]
                and ctx.budget.total_left()
                and ctx.budget.remaining_seconds() >= NEXT_ISSUE_MIN_SECONDS
            )
            s.detail = {"gaps": gaps, "recheck": recheck}
        if recheck:
            ctx.budget.start_issue()  # 재조사는 문제당 호출 수를 새로 센다
            note = "[재조사] 보고서 전에 다음을 확인하세요:\n" + "\n".join(f"- {g}" for g in gaps)
            return {
                "messages": state["messages"] + [HumanMessage(note)],
                "rechecked": True,
                "recheck": True,
                "report_attempts": 0,
            }
        exhausted = state["limited"] or not ctx.budget.total_left() or ctx.budget.issue_exhausted()
        reason = (
            "확인하지 못함 (예산 소진)" if exhausted else "확인하지 못함 (재조사 후에도 근거 없음)"
        )
        report = downgrade_unresolved_cause(mark_unobserved(report, obs, reason), obs)
        report = judge(report, cause_fetched=cause_fetched(report, obs), sources=sources(state))
        return {"report": report, "recheck": False}

    def sources(state: InvestigationState) -> dict[str, str]:
        """근거 번호 → 출처 종류. 신호판·실패 증상·성공한 도구 호출만 (판단 수준, #106)."""
        req = state["request"]
        out = {
            s.evidence_id: SIGNAL_SOURCE
            for ss in req.signal_board.by_service.values()
            for s in ss
            if s.evidence_id
        }
        out |= {x.evidence_id: SYMPTOM_SOURCE for x in req.symptoms if x.evidence_id}
        out |= {
            o.evidence_id: SOURCE_OF_TOOL[o.tool]
            for o in state["observations"]
            if o.ok and o.tool in SOURCE_OF_TOOL
        }
        return out

    def after_stop_check(state: InvestigationState) -> str:
        return "act" if state["recheck"] else END

    def after_tools(state: InvestigationState) -> str:
        return "limit_time" if ctx.budget.time_over() else "act"

    def limit_time(state: InvestigationState) -> InvestigationState:
        return {"limited": not state["rechecked"]}

    g = StateGraph(InvestigationState)
    for name, fn in [
        ("load_context", load_context),
        ("preliminary", preliminary),
        ("triage", triage),
        ("plan", plan),
        ("act", act),
        ("next_issue", next_issue),
        ("tools", run_tools),
        ("limit", limit),
        ("skip_calls", skip_calls),
        ("limit_time", limit_time),
        ("write_report", write_report),
        ("validate", validate),
        ("stop_check", stop_check),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "preliminary")
    g.add_edge("preliminary", "triage")
    g.add_edge("triage", "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges(
        "act", after_act, ["tools", "limit", "skip_calls", "next_issue", "write_report"]
    )
    g.add_edge("next_issue", "plan")
    g.add_conditional_edges("tools", after_tools, ["act", "limit_time"])
    g.add_edge("limit", "write_report")
    g.add_conditional_edges("skip_calls", after_skip_calls, ["next_issue", "write_report"])
    g.add_edge("limit_time", "write_report")
    g.add_edge("write_report", "validate")
    g.add_conditional_edges("validate", after_validate, ["write_report", "stop_check"])
    g.add_conditional_edges("stop_check", after_stop_check, ["act", END])
    return g.compile()


def _parse_result(content: str) -> tuple[str | None, bool]:
    """어댑터가 감싼 도구 결과에서 evidence_id와 ok를 꺼낸다."""
    inner = content.removeprefix("<tool_output>").removesuffix("</tool_output>")
    try:
        body = json.loads(inner)
        return body.get("evidence_id"), bool(body.get("ok", True))
    except (json.JSONDecodeError, AttributeError):
        return None, False
