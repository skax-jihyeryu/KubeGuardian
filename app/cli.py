"""KubeGuardian CLI (개발·평가·CronJob 진입). 실행: uv run python app/cli.py check

kg investigate ["증상"]: kg check와 같은 점검 뒤 게이트가 열렸으면(경로 A·B) 조사하고
  보고서를 요약한다.
  증상 문구를 주면 사용자 증상으로 게이트(경로 B)와 조사 요청에 전달한다. 경로 C면 조사하지 않는다.
kg check: 규칙 점검과 증상 감지(API 시나리오)를 한 번 돌리고
규칙별 판정 결과·판정에 쓴 값·증상 결과·게이트 경로를 출력한다.
종료 코드: 0 점검 완료(게이트 경로와 무관, 조사는 완료 또는 상한 도달), 2 입력 오류,
1 서버 오류(ServiceException.exit_code) 또는 조사 실패(investigate의 조사 상태 failed).
다른 점검이 진행 중이면(점검 잠금) 주기 실행(--trigger schedule)은 건너뜀을 출력하고 0,
직접 실행은 CHECK_ALREADY_RUNNING(2)이다.
"""

import argparse
import json
import re
import signal
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.accounts import load_accounts
from core.config import KubeGuardianConfig, get_setting, load_kubeguardian_config
from core.db import InvestigationQueue, create_db_engine
from core.db.investigation_queue import Job
from core.error.service_exception import ErrorCode, ServiceException
from core.http import NamespacedHttp
from core.llm import build_chat_model
from service.agent.base import AgentResult
from service.chat import answer_question, format_answer
from service.checks.detectors.catalog import CATALOG
from service.notify.notifier import Notifier
from service.notify.sender import Sender, build_sender
from service.pipeline.check import CheckOutcome, clean_symptom, run_check
from service.pipeline.investigate import queue_investigations, run_investigation
from service.pipeline.worker import STALE_CLAIM, Worker, complete_job
from service.sensors.collector import KubeReader

DEFAULT_DB = "kubeguardian.db"
WORKER_POLL_S = 5  # 조사 대기를 보는 간격. 원인 파악 7분(기획서 §9.2)에 비해 작다
VERDICT_LABEL = {
    "detected": "적발",
    "observing": "관찰 중",
    "passed": "통과",
    "unknown": "확인하지 못함",
}

INCIDENT_LABEL = {
    "opened": "새로 열림 (조사 대기)",
    "continued": "진행 중 (같은 지문)",
    "changed": "지문 바뀜 (같은 이슈에서 재조사)",
    "seen": "열려 있음 (직접 점검은 이슈를 바꾸지 않음)",
    "resolved": "해결",
}

SenderFactory = Callable[[KubeGuardianConfig], Sender]
ReaderFactory = Callable[[KubeGuardianConfig], KubeReader]
HttpFactory = Callable[[KubeGuardianConfig], Any]
LlmFactory = Callable[[str], BaseChatModel]
MAX_SYMPTOM_LENGTH = 500
CONVERSATION_ID_PATTERN = r"[A-Za-z0-9_-]{1,32}"


def _http(config: KubeGuardianConfig) -> NamespacedHttp:
    accounts = load_accounts(get_setting().KG_ACCOUNTS_DIR)
    return NamespacedHttp(config.target.namespace, accounts=accounts)


def _k8s_reader(config: KubeGuardianConfig) -> KubeReader:
    from core.k8s import K8sClient

    try:
        return K8sClient.from_config(config)
    except Exception as e:  # kubeconfig 없음·클러스터 밖 등
        raise ServiceException(ErrorCode.COLLECT_FAILED, type(e).__name__) from e


def _load_config(path: str) -> KubeGuardianConfig:
    try:
        return load_kubeguardian_config(path)
    except FileNotFoundError as e:
        raise ServiceException(ErrorCode.BAD_REQUEST, f"설정 파일 없음: {path}") from e
    except ValidationError as e:
        raise ServiceException(ErrorCode.VALIDATION_ERROR, f"설정 파일 검증 실패: {path}") from e


def _open_db(path: str) -> Engine:
    try:
        return create_db_engine(path)
    except Exception as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e


def check(
    args: argparse.Namespace,
    make_reader: ReaderFactory,
    make_http: HttpFactory,
    make_sender: SenderFactory,
) -> None:
    config = _load_config(args.config)
    reader = make_reader(config)
    engine = _open_db(args.db)
    outcome = run_check(reader, config, engine, trigger=args.trigger, http=make_http(config))
    print_outcome(outcome)
    notifier = Notifier(engine, config, make_sender(config))
    _notify(lambda: notifier.after_check(outcome.run_id, outcome.gate, outcome.incidents, _now()))
    _queue(engine, config, outcome, args.trigger)  # 1차 알림 뒤에 (작업자가 집는다)
    print_alerts(notifier)


def investigate(
    args: argparse.Namespace,
    make_reader: ReaderFactory,
    make_http: HttpFactory,
    make_llm: LlmFactory,
    make_sender: SenderFactory,
) -> int:
    symptom = args.symptom  # 공백 처리는 run_check·build_request가 한다
    if len(clean_symptom(symptom) or "") > MAX_SYMPTOM_LENGTH:
        raise ServiceException(
            ErrorCode.VALIDATION_ERROR, f"증상은 {MAX_SYMPTOM_LENGTH}자 이하로 쓴다"
        )
    config = _load_config(args.config)
    reader, http = make_reader(config), make_http(config)
    engine = _open_db(args.db)
    outcome = run_check(
        reader, config, engine, trigger=args.trigger, http=http, user_symptom=symptom
    )
    print_outcome(outcome)
    notifier = Notifier(engine, config, make_sender(config))
    # 1차 감지 알림은 AI 조사를 기다리지 않는다
    _notify(lambda: notifier.after_check(outcome.run_id, outcome.gate, outcome.incidents, _now()))
    _queue(engine, config, outcome, args.trigger, symptom)
    jobs = _claim_jobs(engine, outcome) if args.trigger == "schedule" else []
    if args.trigger == "schedule" and outcome.gate.path != "C" and not jobs:
        print("\n조사 생략: 이번 점검이 넣은 조사 대기가 없다 (같은 지문이거나 작업자가 이미 맡음)")
        print_alerts(notifier)
        return 0
    result = run_investigation(
        outcome,
        config,
        engine,
        run_id=f"kg-investigate-{outcome.run_id}",
        trigger=args.trigger,
        llm_factory=make_llm,
        k8s=reader,
        http=http,
        symptom=symptom,
    )
    print_investigation(result)
    try:
        for job in jobs:
            complete_job(engine, notifier, job, result, _now())
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    print_alerts(notifier)
    return 1 if result is not None and result.status == "failed" else 0


def ask(args: argparse.Namespace, make_llm: LlmFactory) -> None:
    config = _load_config(args.config)
    engine = _open_db(args.db)
    reply = answer_question(
        args.question,
        engine=engine,
        config=config,
        llm_factory=make_llm,
        conversation_id=args.conversation,
    )
    print(f"대화 {reply.conversation_id}")
    print(format_answer(reply.answer))


def _conversation_id(value: str) -> str:
    if not re.fullmatch(CONVERSATION_ID_PATTERN, value):
        raise argparse.ArgumentTypeError("영문·숫자·_·-로 1~32자")
    return value


def worker(
    args: argparse.Namespace,
    make_reader: ReaderFactory,
    make_http: HttpFactory,
    make_llm: LlmFactory,
    make_sender: SenderFactory,
) -> int:
    """대기열이 비면 WORKER_POLL_S만큼 쉬고 다시 본다. SIGTERM이면 지금 조사를 끝내지 않고 멈춘다
    (집힘은 만료 뒤 다른 작업자가 다시 집는다)."""
    config = _load_config(args.config)
    engine = _open_db(args.db)
    notifier = Notifier(engine, config, make_sender(config))
    work = Worker(
        engine,
        config,
        notifier,
        llm_factory=make_llm,
        make_k8s=lambda: make_reader(config),
        make_http=lambda: make_http(config),
    )
    print(f"작업자 시작: {args.db} (알림 {'dry-run' if notifier.dry_run else 'Slack'})", flush=True)
    while True:
        try:
            done = work.run_once()
        except SQLAlchemyError as e:  # DB가 잠깐 잠겨도 작업자는 계속 돈다
            print(f"저장소 오류: {type(e).__name__}", file=sys.stderr, flush=True)
            done = None
        if done is not None:
            print(f"이슈 #{done} 조사 끝", flush=True)
        if args.once:
            return 0
        if done is None:
            time.sleep(WORKER_POLL_S)


def _now() -> datetime:
    return datetime.now(UTC)


def _notify(send: Callable[[], None]) -> None:
    """알림이 어떤 이유로 실패해도 점검·조사를 멈추지 않는다 (발송 실패는 이력에 남는다)."""
    try:
        send()
    except Exception as e:  # noqa: BLE001 — 알림은 부가 기능이다
        print(f"알림 실패: {type(e).__name__}", file=sys.stderr)


def print_alerts(notifier: Notifier) -> None:
    if notifier.sent:
        mode = " (dry-run)" if notifier.dry_run else ""
        print("\n" + "\n".join(f"알림: {kind}{mode}" for kind in notifier.sent))


def _queue(
    engine: Engine,
    config: KubeGuardianConfig,
    outcome: CheckOutcome,
    trigger: str,
    symptom: str | None = None,
) -> None:
    try:
        queue_investigations(engine, config, outcome, trigger=trigger, now=_now(), symptom=symptom)
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e


def _claim_jobs(engine: Engine, outcome: CheckOutcome) -> list[Job]:
    """이번 점검이 넣은 조사 대기를 집는다 (작업자와 겹치지 않게).

    다른 점검이 넣은 요청은 그 점검의 근거에 묶여 있어 작업자에게 맡긴다.
    """
    queue = InvestigationQueue(engine)
    ids = dict.fromkeys(c.incident_id for c in outcome.incidents if c.needs_investigation)
    try:
        jobs = [
            queue.claim(_now(), STALE_CLAIM, incident_id=i, check_run_id=outcome.run_id)
            for i in ids
        ]
    except SQLAlchemyError as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    return [j for j in jobs if j is not None]


def print_investigation(result: AgentResult | None) -> None:
    if result is None:
        print("\n조사하지 않음 (경로 C)")
        return
    print(f"\n조사 상태: {result.status}")
    if result.error:
        print(f"오류: {result.error}")
    report = result.report
    if report is None:
        return
    print(f"결론: {report.conclusion} (신뢰도 {report.confidence})")
    if top := report.top_issue:
        print(f"1순위: {top.title} ({top.severity})")
    print(f"근거 {len(report.evidence_ids)}건")


def print_outcome(out: CheckOutcome) -> None:
    snap = out.snapshot
    print(f"점검 #{out.run_id}  {snap.namespace}  {snap.collected_at.isoformat()}")
    for kind, reason in snap.failed.items():
        print(f"수집 실패: {kind} ({reason})")
    for rule in CATALOG.values():
        print(f"\n{rule.id} {rule.name}")
        signals = [s for s in out.signals if s.rule_id == rule.id]
        if not signals:
            print("  대상 없음")
        for s in signals:
            label = VERDICT_LABEL[s.verdict] + (f"({s.severity})" if s.severity else "")
            line = f"  {label}  {s.subject}"
            if s.values:
                line += "  " + json.dumps(s.values, ensure_ascii=False, sort_keys=True)
            if s.message:
                line += f"  {s.message}"
            print(line)
    if out.symptoms:
        print("\n증상 감지")
        for sym in out.symptoms:
            label = {
                "passed": "성공",
                "failed": f"실패({sym.severity})",
                "unknown": "확인하지 못함",
            }[sym.status]
            print(f"  {label}  {sym.scenario}  {sym.summary}")
    gate = out.gate
    severity = f" ({gate.severity})" if gate.severity else ""
    print(f"\n게이트: 경로 {gate.path}{severity} — {gate.reason}")
    for c in out.incidents:
        print(f"이슈 #{c.incident_id} {INCIDENT_LABEL[c.action]}")


def main(
    argv: Sequence[str] | None = None,
    *,
    make_reader: ReaderFactory = _k8s_reader,
    make_http: HttpFactory = _http,
    make_llm: LlmFactory = build_chat_model,
    make_sender: SenderFactory | None = None,
) -> int:
    make_sender = make_sender or (lambda cfg: build_sender(get_setting(), cfg))
    parser = argparse.ArgumentParser(prog="kg", description="KubeGuardian CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="규칙 점검을 한 번 실행하고 결과를 출력한다")
    p_check.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_check.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    p_check.add_argument(
        "--trigger",
        choices=["check", "schedule"],
        default="check",
        help="실행 방식 (CronJob은 schedule)",
    )
    p_inv = sub.add_parser("investigate", help="점검 뒤 게이트가 열리면 조사 에이전트를 실행한다")
    p_inv.add_argument("symptom", nargs="?", help="사용자 증상 (선택, 최대 500자)")
    p_inv.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_inv.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    p_inv.add_argument(
        "--trigger",
        choices=["investigate", "schedule"],
        default="investigate",
        help="실행 방식 (기본: investigate)",
    )
    p_ask = sub.add_parser("ask", help="저장된 점검·이슈·알림 이력에 대해 질문한다")
    p_ask.add_argument("question", help="질문 (최대 500자)")
    p_ask.add_argument(
        "--conversation", type=_conversation_id, help="이어갈 대화 ID (없으면 새 대화)"
    )
    p_ask.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_ask.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    p_work = sub.add_parser(
        "worker", help="조사 대기 이슈를 집어 조사하고 점검 중단을 감시한다 (diagnostic-agent)"
    )
    p_work.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_work.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (CronJob과 같은 파일)")
    p_work.add_argument("--once", action="store_true", help="한 번만 처리하고 끝낸다 (확인용)")
    args = parser.parse_args(argv)
    try:
        if args.command == "worker":
            return worker(args, make_reader, make_http, make_llm, make_sender)
        if args.command == "check":
            check(args, make_reader, make_http, make_sender)
        elif args.command == "investigate":
            return investigate(args, make_reader, make_http, make_llm, make_sender)
        elif args.command == "ask":
            ask(args, make_llm)
    except ServiceException as e:
        if (
            e.error_code is ErrorCode.CHECK_ALREADY_RUNNING
            and getattr(args, "trigger", "") == "schedule"
        ):
            print(f"건너뜀: 다른 점검이 진행 중이다 ({e.detail})")  # CronJob 실패로 보이지 않게
            return 0
        print(
            f"오류 {e.error_code.code} {e.error_code.name}: {e.error_code.message}", file=sys.stderr
        )
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return e.exit_code
    return 0


def _exit_on_sigterm() -> None:
    """SIGTERM(CronJob activeDeadlineSeconds·Pod 종료)을 SystemExit로 바꿔 finally가 돌게 한다.

    기본 처리는 바로 끝나서 run_check의 점검 잠금을 놓지 못한다.
    """
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))


if __name__ == "__main__":
    _exit_on_sigterm()
    sys.exit(main())
