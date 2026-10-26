"""investigator v1 그래프: 가짜 LLM(각본)과 가짜 도구로 흐름·상한·근거 검증을 확인한다."""

import json
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from core.guard.budget import Budget, BudgetTracker
from service.agent.base import RunContext
from service.agent.investigator.v1.agent import InvestigatorV1
from service.agent.investigator.v1.graph import build_graph
from service.model import Evidence, InvestigationRequest, Signal, SignalBoard, Symptom
from service.tools.base import Tool, ToolContext, ToolObservation

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


class MemoryEvidence:
    def __init__(self):
        self.saved: list[Evidence] = []

    def add(self, evidence: Evidence) -> str:
        self.saved.append(evidence)
        return f"ev-{len(self.saved)}"


class ScriptedLLM(BaseChatModel):
    """각본의 응답을 차례로 돌려준다. 구조화 출력은 지원하지 않아 텍스트 파싱 경로를 탄다."""

    script: list[Any]
    calls: list[list] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = item if isinstance(item, AIMessage) else AIMessage(content=item)
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _fake_tool(tool_name: str):
    class Fake(Tool):
        name = tool_name
        description = f"{tool_name} 테스트 도구"

        class Input(BaseModel):
            kind: str | None = None
            name: str | None = None
            since_minutes: int | None = None

        def collect(self, args, ctx: ToolContext) -> ToolObservation:
            return ToolObservation(
                kind="resource",
                subject=f"dev/Service/{args.name or 'all'}",
                source=f"GET {tool_name}",
                summary=f"{tool_name} 결과",
                data={"ok": True},
            )

    return Fake()


TOOLS = [_fake_tool(n) for n in ("get_topology", "get_events", "get_resource")]

TRIAGE = json.dumps(
    {
        "issues": [
            {
                "rank": 1,
                "title": "gateway 주소 설정 오류",
                "impact": "core_path_broken",
                "confidence": "high",
                "evidence_ids": [],
            }
        ],
        "runbook": "service-unreachable",
    }
)


PLAN = json.dumps(
    {
        "hypotheses": [
            {
                "statement": "gateway 주소 env가 localhost",
                "area": "config",
                "check": "gateway Deployment env 확인",
            }
        ],
        "order": ["config", "wiring", "run", "logs"],
    }
)


def _tool_call(i: int, name: str = "get_resource", args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args or {"kind": "Service", "name": "gateway"}, "id": f"c{i}"}
        ],
    )


def _report(evidence_ids: list[str]) -> str:
    return json.dumps(
        {
            "conclusion": "gateway 주소 env가 localhost",
            "confidence": "high",
            "impact_scope": "전체",
            "evidence_ids": evidence_ids,
        }
    )


REQ = InvestigationRequest(
    trigger="check",
    gate_path="A",
    signal_board=SignalBoard(
        by_service={
            "gateway": [
                Signal(
                    rule_id="S04",
                    verdict="detected",
                    subject="dev/Service/gateway",
                    severity="critical",
                    evidence_id="ev-s04",
                )
            ]
        }
    ),
)


def _run(script: list, budget: Budget | None = None, clock=None):
    llm = ScriptedLLM(script=script, calls=[])
    tracker = (
        BudgetTracker(budget or Budget(), clock=clock)
        if clock
        else BudgetTracker(budget or Budget())
    )
    ctx = RunContext(
        run_id="r1", namespace="dev", evidence=MemoryEvidence(), budget=tracker, now=lambda: NOW
    )
    result = InvestigatorV1(llm_factory=lambda model: llm, tools=TOOLS, ctx=ctx).run(REQ)
    return result, llm, ctx


def test_graph_runs_end_to_end_and_report_cites_saved_evidence():
    # 사전 확인 2회(ev-1, ev-2) → 선별 → 도구 1회(ev-3) → 종료 → 보고서
    result, _, ctx = _run(
        [TRIAGE, PLAN, _tool_call(1), AIMessage(content="조사 완료"), _report(["ev-3", "ev-99"])]
    )

    assert result.status == "done", result.error
    assert result.report.evidence_ids == ["ev-3"]  # 없는 번호는 검증기가 지운다
    assert len(ctx.evidence.saved) == 3
    assert [f.evidence_ids for f in result.report.preliminary_facts] == [["ev-1"], ["ev-2"]]


def test_tool_budget_stops_loop_and_marks_incomplete():
    # 도구 2회(문제당 상한) 뒤 세 번째 요청은 실행하지 않고 보고서로 간다
    script = [TRIAGE, PLAN] + [_tool_call(i) for i in range(3)] + [_report(["ev-3"])]
    result, llm, _ = _run(script, Budget(per_issue_tool_calls=2, total_tool_calls=20))

    assert result.status == "incomplete"
    assert result.usage.tool_calls == 4  # 사전 확인 2 + 조사 2
    assert result.report is not None


def test_time_limit_marks_incomplete():
    ticks = iter([0, 0, 0, 0, 200, 200, 200, 200, 200, 200, 200, 200, 200])
    result, _, _ = _run(
        [TRIAGE, PLAN, _tool_call(1), _report(["ev-1"])], clock=lambda: next(ticks, 200)
    )

    assert result.status == "incomplete"


def test_report_without_evidence_is_rewritten_once():
    result, llm, _ = _run(
        [TRIAGE, PLAN, AIMessage(content="조사 완료"), _report([]), _report(["ev-1"])]
    )

    assert result.status == "done"
    assert result.report.evidence_ids == ["ev-1"]
    assert result.report.conclusion_supported is True
    assert [s.node for s in result.steps].count("write_report") == 2


def test_report_still_without_evidence_gets_low_confidence():
    result, _, _ = _run([TRIAGE, PLAN, AIMessage(content="조사 완료"), _report([]), _report([])])

    assert result.status == "done"
    assert result.report.confidence == "low"
    assert result.report.conclusion_supported is False


def test_llm_error_becomes_failed():
    result, _, _ = _run([TRIAGE, PLAN, RuntimeError("LLM 연결 실패")])

    assert result.status == "failed"


def test_unknown_tool_call_returns_error_message_and_loop_continues():
    result, llm, _ = _run(
        [
            TRIAGE,
            PLAN,
            _tool_call(1, name="kubectl_exec"),
            AIMessage(content="조사 완료"),
            _report(["ev-1"]),
        ]
    )

    assert result.status == "done"
    tool_msg = llm.calls[3][-1]
    assert "kubectl_exec" in tool_msg.content and "ok" in tool_msg.content


def test_steps_recorded_per_node():
    result, _, _ = _run(
        [TRIAGE, PLAN, _tool_call(1), AIMessage(content="조사 완료"), _report(["ev-3"])]
    )

    nodes = [s.node for s in result.steps]
    for node in ("preliminary", "triage", "act", "tools", "write_report", "validate"):
        assert node in nodes
    tool_step = next(s for s in result.steps if s.node == "tools")
    assert tool_step.tool == "get_resource" and tool_step.evidence_ids == ["ev-3"]


def test_list_content_reply_is_read_as_text():
    """Responses API는 content를 블록 목록으로 준다."""
    listed = AIMessage(
        content=[{"type": "reasoning", "summary": []}, {"type": "text", "text": TRIAGE}]
    )
    result, _, _ = _run([listed, PLAN, AIMessage(content="조사 완료"), _report(["ev-1"])])

    assert result.status == "done"


def test_parallel_tool_calls_over_budget_mark_incomplete():
    # 한 응답에 호출 3개, 문제당 상한 2 -> 2회만 실행되고 상한에 걸린 것으로 본다
    three = AIMessage(
        content="",
        tool_calls=[
            {"name": "get_resource", "args": {"kind": "Service", "name": f"s{i}"}, "id": f"c{i}"}
            for i in range(3)
        ],
    )
    script = [TRIAGE, PLAN, three, AIMessage(content="조사 완료"), _report(["ev-3"])]
    result, _, _ = _run(script, Budget(per_issue_tool_calls=2, total_tool_calls=20))

    assert result.usage.tool_calls == 4  # 사전 확인 2 + 조사 2
    assert result.status == "incomplete"


def test_path_b_without_signals_runs_scenario_in_preliminary():
    scenario_tool = _fake_tool("run_api_scenario")
    scenario_tool.scenario_names = ["login-chat", "second"]
    req = InvestigationRequest(
        trigger="check",
        gate_path="B",
        signal_board=SignalBoard(),
        user_symptom="채팅이 안 돼요",
    )
    llm = ScriptedLLM(
        script=[TRIAGE, PLAN, AIMessage(content="조사 완료"), _report(["ev-3"])], calls=[]
    )
    ctx = RunContext(
        run_id="r1",
        namespace="dev",
        evidence=MemoryEvidence(),
        budget=BudgetTracker(Budget()),
        now=lambda: NOW,
    )

    result = InvestigatorV1(
        llm_factory=lambda model: llm, tools=[*TOOLS, scenario_tool], ctx=ctx
    ).run(req)

    assert result.status == "done", result.error
    assert [s.tool for s in result.steps if s.node == "tools"] == []
    subjects = [e.subject for e in ctx.evidence.saved]
    assert len(subjects) == 3 and ctx.evidence.saved[2].source == "GET run_api_scenario"
    assert "채팅이 안 돼요" in llm.calls[0][-1].content  # 선별이 증상으로 진행


def test_path_a_preliminary_does_not_run_scenario():
    scenario_tool = _fake_tool("run_api_scenario")
    scenario_tool.scenario_names = ["login-chat"]
    req = InvestigationRequest(
        trigger="check",
        gate_path="A",
        signal_board=SignalBoard(),
        user_symptom="채팅이 안 돼요",
    )
    llm = ScriptedLLM(
        script=[TRIAGE, PLAN, AIMessage(content="조사 완료"), _report(["ev-1"])], calls=[]
    )
    ctx = RunContext(
        run_id="r1",
        namespace="dev",
        evidence=MemoryEvidence(),
        budget=BudgetTracker(Budget()),
        now=lambda: NOW,
    )

    result = InvestigatorV1(
        llm_factory=lambda model: llm, tools=[*TOOLS, scenario_tool], ctx=ctx
    ).run(req)

    assert result.status == "done", result.error
    sources = [e.source for e in ctx.evidence.saved]
    # 사전 확인은 토폴로지·이벤트뿐
    assert len(sources) == 2 and "GET run_api_scenario" not in sources


def test_path_b_reruns_the_failed_scenario_and_shows_it_in_triage():
    scenario_tool = _fake_tool("run_api_scenario")
    scenario_tool.scenario_names = ["login-chat", "second"]  # 두 번째만 실패했다
    failed = Symptom(
        scenario="second",
        status="failed",
        failed_step=2,
        summary="시나리오 second: 3단계 중 2단계 실패 (call /x → 500)",
        severity="critical",
        evidence_id="ev-9",
    )
    req = InvestigationRequest(
        trigger="check", gate_path="B", signal_board=SignalBoard(), symptoms=[failed]
    )
    llm = ScriptedLLM(
        script=[TRIAGE, PLAN, AIMessage(content="조사 완료"), _report(["ev-9"])], calls=[]
    )
    ctx = RunContext(
        run_id="r1",
        namespace="dev",
        evidence=MemoryEvidence(),
        budget=BudgetTracker(Budget()),
        now=lambda: NOW,
    )

    result = InvestigatorV1(
        llm_factory=lambda model: llm, tools=[*TOOLS, scenario_tool], ctx=ctx
    ).run(req)

    assert result.status == "done", result.error
    assert ctx.evidence.saved[2].subject == "dev/Service/second"  # 첫 시나리오가 아니라 실패한 것
    triage_prompt = llm.calls[0][-1].content
    assert "second" in triage_prompt and "2단계" in triage_prompt and "ev-9" in triage_prompt
    assert "증상(API 시나리오 실패 또는 사용자 입력)" in triage_prompt
    assert result.report.evidence_ids == ["ev-9"]  # 점검 때의 증상 근거는 인용할 수 있다


def _triage(*titles: str) -> str:
    return json.dumps(
        {
            "issues": [
                {
                    "rank": n,
                    "title": t,
                    "impact": "core_path_broken",
                    "confidence": "high",
                    "evidence_ids": [],
                }
                for n, t in enumerate(titles, 1)
            ],
            "runbook": "service-unreachable",
        }
    )


def test_plan_is_injected_into_investigate_prompt():
    _, llm, _ = _run([TRIAGE, PLAN, AIMessage(content="조사 완료"), _report(["ev-1"])])

    first = llm.calls[2][-1].content  # triage, plan 다음의 첫 act
    assert "gateway 주소 env가 localhost" in first
    assert "gateway Deployment env 확인" in first


def test_second_issue_investigated_when_budget_remains():
    script = [
        _triage("첫째 문제", "둘째 문제"),
        PLAN,
        AIMessage(content="조사 완료"),
        PLAN,
        AIMessage(content="조사 완료"),
        _report(["ev-1"]),
    ]
    result, llm, _ = _run(script)

    assert result.status == "done", result.error
    second_act = llm.calls[4]  # triage, plan, act, plan, act
    assert "둘째 문제" in second_act[-1].content
    assert any("첫째 문제" in str(m.content) for m in second_act)  # 앞 대화가 이어진다
    report_prompt = llm.calls[5][-1].content
    assert "첫째 문제" in report_prompt and "둘째 문제" in report_prompt
    assert [s.node for s in result.steps].count("plan") == 2


def test_second_issue_skipped_when_time_short():
    # 전체 30초 예산이면 남은 시간이 40초 미만이라 다음 문제로 가지 않는다
    script = [
        _triage("첫째 문제", "둘째 문제"),
        PLAN,
        AIMessage(content="조사 완료"),
        _report(["ev-1"]),
    ]
    result, llm, _ = _run(script, Budget(seconds=30))

    assert [s.node for s in result.steps].count("plan") == 1
    assert "(예산 부족으로 조사하지 못함)" in llm.calls[-1][-1].content


def test_no_issues_runs_without_plan_llm_call():
    script = [_triage(), AIMessage(content="조사 완료"), _report(["ev-1"])]
    result, llm, _ = _run(script)

    assert result.status == "done", result.error
    assert len(llm.calls) == 3  # triage, act, report. plan LLM 호출 없음


def test_observations_record_get_resource_kind_and_name():
    llm = ScriptedLLM(
        script=[
            TRIAGE,
            PLAN,
            _tool_call(1, args={"kind": "Deployment", "name": "gateway"}),
            AIMessage(content="조사 완료"),
            _report(["ev-3"]),
        ],
        calls=[],
    )
    ctx = RunContext(
        run_id="r1",
        namespace="dev",
        evidence=MemoryEvidence(),
        budget=BudgetTracker(Budget()),
        now=lambda: NOW,
    )
    agent = InvestigatorV1(llm_factory=lambda model: llm, tools=TOOLS, ctx=ctx)

    state = build_graph(agent).invoke({"request": REQ}, {"recursion_limit": 100})

    got = [(o.evidence_id, o.tool, o.kind, o.name) for o in state["observations"]]
    assert ("ev-3", "get_resource", "Deployment", "gateway") in got
    assert ("ev-1", "get_topology", None, None) in got
