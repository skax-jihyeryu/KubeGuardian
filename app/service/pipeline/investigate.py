"""조사 실행 흐름: 점검 결과 → 조사 요청 → 에이전트 실행 (상세설계 §2.1).

평가 러너·CLI·Slack 명령·주기 점검이 같은 함수를 쓴다 (실행 계획서 §1.2).
게이트가 경로 C(정상)면 조사하지 않는다.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from sqlalchemy import Engine

from core.config import KubeGuardianConfig
from core.db import EvidenceStore
from core.guard.budget import BudgetTracker
from core.guard.redact import redact_text
from core.llm import build_chat_model
from core.tracing import setup_tracing
from service.agent import registry
from service.agent.base import AgentResult, RunContext
from service.model import InvestigationRequest, Topology, Trigger
from service.pipeline.check import CheckOutcome
from service.sensors.topology import build_topology
from service.tools import build_tools

AGENT_KEY = "investigator:v1"


def build_request(
    outcome: CheckOutcome,
    config: KubeGuardianConfig,
    *,
    trigger: Trigger,
    symptom: str | None = None,
) -> InvestigationRequest | None:
    if outcome.gate.path == "C":
        return None
    topology = build_topology(outcome.snapshot, config) or Topology()
    return InvestigationRequest(
        trigger=trigger,
        gate_path=outcome.gate.path,
        signal_board=outcome.board,
        topology=topology,
        symptoms=outcome.gate.symptoms,
        user_symptom=redact_text(symptom) if symptom else None,
    )


def run_investigation(
    outcome: CheckOutcome,
    config: KubeGuardianConfig,
    engine: Engine,
    *,
    run_id: str,
    trigger: Trigger = "check",
    llm_factory: Callable[[str], BaseChatModel] = build_chat_model,
    k8s: Any = None,
    http: Any = None,
    agent_key: str = AGENT_KEY,
    symptom: str | None = None,
) -> AgentResult | None:
    """게이트가 열렸으면 조사한다. 경로 C면 None."""
    request = build_request(outcome, config, trigger=trigger, symptom=symptom)
    if request is None:
        return None
    setup_tracing()
    agent_cls = registry.get(agent_key)
    ctx = RunContext(
        run_id=run_id,
        namespace=config.target.namespace,
        evidence=EvidenceStore(engine, check_run_id=outcome.run_id),
        budget=BudgetTracker(agent_cls.spec.budget),
        k8s=k8s,
        http=http,
    )
    agent = agent_cls(llm_factory=llm_factory, tools=build_tools(config), ctx=ctx)
    return agent.run(request)
