"""조사 실행 흐름: 점검 결과 → 조사 요청 → 에이전트 실행 (상세설계 §2.1).

평가 러너·CLI·Slack 명령·주기 점검이 같은 함수를 쓴다 (실행 계획서 §1.2).
게이트가 경로 C(정상)면 조사하지 않는다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from opentelemetry import trace
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.config import KubeGuardianConfig
from core.db import EvidenceStore, InvestigationQueue, InvestigationStore
from core.db.investigations import INTERRUPTED
from core.guard.budget import BudgetTracker
from core.guard.judgement import judge
from core.guard.redact import redact_text
from core.llm import build_chat_model
from core.log.logging import get_logging
from core.tracing import setup_tracing
from service.agent import registry
from service.agent.base import AgentResult, RunContext
from service.model import InvestigationRequest, Topology, Trigger
from service.pipeline.check import CheckOutcome, clean_symptom
from service.pipeline.flow_log import FlowLogSteps, log_end, log_start
from service.sensors.topology import build_topology
from service.tools import build_tools

AGENT_KEY = "investigator:v1"

logger = get_logging()


def build_request(
    outcome: CheckOutcome,
    config: KubeGuardianConfig,
    *,
    trigger: Trigger,
    symptom: str | None = None,
) -> InvestigationRequest | None:
    if outcome.gate.path == "C":
        return None
    symptom = clean_symptom(symptom)
    topology = build_topology(outcome.snapshot, config) or Topology()
    return InvestigationRequest(
        trigger=trigger,
        gate_path=outcome.gate.path,
        signal_board=outcome.board,
        topology=topology,
        symptoms=outcome.gate.symptoms,
        user_symptom=redact_text(symptom) if symptom else None,
    )


def queue_investigations(
    engine: Engine,
    config: KubeGuardianConfig,
    outcome: CheckOutcome,
    *,
    trigger: Trigger,
    now: datetime,
    symptom: str | None = None,
) -> None:
    """새 이슈·지문 변경이면 점검 시점의 조사 요청을 대기열에 넣는다 (작업자가 집는다, #28).

    1차 감지 알림(M1)을 보낸 **뒤에** 부른다. 먼저 넣으면 작업자가 M1보다 먼저 조사를 끝낼 수 있다.
    """
    changed = [c.incident_id for c in outcome.incidents if c.needs_investigation]
    request = build_request(outcome, config, trigger=trigger, symptom=symptom) if changed else None
    if request is None:
        return
    queue = InvestigationQueue(engine)
    body = request.model_dump(mode="json")
    for incident_id in changed:
        queue.put(incident_id, outcome.run_id, body, now)


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
    incident_id: int | None = None,
) -> AgentResult | None:
    """게이트가 열렸으면 조사한다. 경로 C면 None."""
    request = build_request(outcome, config, trigger=trigger, symptom=symptom)
    if request is None:
        return None
    return run_request(
        request,
        config,
        engine,
        check_run_id=outcome.run_id,
        run_id=run_id,
        llm_factory=llm_factory,
        k8s=k8s,
        http=http,
        agent_key=agent_key,
        incident_id=incident_id,
        gate_reason=outcome.gate.reason,
    )


def run_request(
    request: InvestigationRequest,
    config: KubeGuardianConfig,
    engine: Engine,
    *,
    check_run_id: int,
    run_id: str,
    llm_factory: Callable[[str], BaseChatModel] = build_chat_model,
    k8s: Any = None,
    http: Any = None,
    agent_key: str = AGENT_KEY,
    incident_id: int | None = None,
    gate_reason: str | None = None,  # 저장된 요청에는 없어 작업자 경로는 None
) -> AgentResult:
    """저장해 둔 조사 요청으로 조사한다 (작업자가 대기열에서 집은 요청, #28).

    근거는 요청을 만든 점검(check_run_id)에 쌓인다. 신호판의 근거 번호를 인용할 수 있게 한다.
    조사와 단계는 investigation·agent_step에 기록한다 (보고서 화면, #23). 단계는 끝날 때마다 쓴다.
    """
    setup_tracing()
    agent_cls = registry.get(agent_key)
    store = InvestigationStore(engine)
    # 조사 span 아래에 LangChain span이 모인다. 추적을 끄면 span이 유효하지 않아 trace_id는 None
    with trace.get_tracer("kubeguardian").start_as_current_span("investigation") as span:
        span_context = span.get_span_context()
        trace_id = trace.format_trace_id(span_context.trace_id) if span_context.is_valid else None
        try:
            investigation_id: int | None = store.start(
                check_run_id=check_run_id,
                incident_id=incident_id,
                run_key=run_id,
                agent=agent_key,
                trigger=request.trigger,
                now=datetime.now(UTC),
                trace_id=trace_id,
                gate_path=request.gate_path,
                gate_reason=gate_reason,
                user_symptom=request.user_symptom,
            )
        except SQLAlchemyError as e:  # 기록 없이 조사한다 (조사가 기록보다 중요하다)
            logger.warning(f"조사 기록 시작 실패: {type(e).__name__}")
            investigation_id = None
        label = _label(investigation_id)
        log_start(label, request, incident_id=incident_id, reason=gate_reason)
        try:
            result = _run_agent(
                agent_cls,
                request,
                config,
                engine,
                store,
                investigation_id,
                check_run_id=check_run_id,
                run_id=run_id,
                llm_factory=llm_factory,
                k8s=k8s,
                http=http,
            )
        except BaseException as e:  # SIGTERM(SystemExit)도 행을 running으로 남기지 않는다
            # 끝을 기록하지 못한 조사와 같은 값 (sweep_stale)
            error = (
                INTERRUPTED if isinstance(e, SystemExit | KeyboardInterrupt) else type(e).__name__
            )
            failed = AgentResult(status="failed", error=error)
            _finish(store, investigation_id, failed)
            log_end(label, failed)
            raise
        _finish(store, investigation_id, result)  # 판단 수준까지 붙은 보고서를 남긴다
        log_end(label, result)
        return result


def _label(investigation_id: int | None) -> str:
    return "-" if investigation_id is None else f"#{investigation_id}"


def _run_agent(
    agent_cls,
    request: InvestigationRequest,
    config: KubeGuardianConfig,
    engine: Engine,
    store: InvestigationStore,
    investigation_id: int | None,
    *,
    check_run_id: int,
    run_id: str,
    llm_factory: Callable[[str], BaseChatModel],
    k8s: Any,
    http: Any,
) -> AgentResult:
    ctx = RunContext(
        run_id=run_id,
        namespace=config.target.namespace,
        evidence=EvidenceStore(engine, check_run_id=check_run_id),
        budget=BudgetTracker(agent_cls.spec.budget),
        k8s=k8s,
        http=http,
        scenario_services={
            s.name: config.core_paths.get(s.feature, []) for s in config.api_scenarios
        },
        steps=FlowLogSteps(
            _label(investigation_id),
            None if investigation_id is None else store.steps_sink(investigation_id),
        ),
    )
    agent = agent_cls(llm_factory=llm_factory, tools=build_tools(config), ctx=ctx)
    result = agent.run(request)
    if (
        result.report is not None
    ):  # 판단 수준은 에이전트가 아니라 여기서 저장된 근거로 정한다 (#106)
        try:
            result.report = judge(result.report, EvidenceStore(engine).get)
        except Exception as e:  # noqa: BLE001 — 판단 수준이 없어도 끝난 보고서를 잃지 않는다
            logger.warning(f"판단 수준 계산 실패: {type(e).__name__}")
    return result


def _finish(store: InvestigationStore, investigation_id: int | None, result: AgentResult) -> None:
    if investigation_id is None:
        return
    try:
        store.finish(investigation_id, result, datetime.now(UTC))
    except SQLAlchemyError as e:  # 완료된 조사를 기록 실패로 잃지 않는다
        logger.warning(f"조사 기록 마감 실패: {type(e).__name__}")
