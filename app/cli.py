"""KubeGuardian CLI (개발·평가·CronJob 진입). 실행: uv run python app/cli.py check

kg investigate ["증상"]: kg check와 같은 점검 뒤 게이트가 열렸으면(경로 A·B) 조사하고
  보고서를 요약한다.
  증상 문구를 주면 사용자 증상으로 게이트(경로 B)와 조사 요청에 전달한다. 경로 C면 조사하지 않는다.
kg check: 규칙 점검과 증상 감지(API 시나리오)를 한 번 돌리고
규칙별 판정 결과·판정에 쓴 값·증상 결과·게이트 경로를 출력한다.
종료 코드: 0 점검 완료(게이트 경로와 무관), 2 입력 오류, 1 서버 오류 (ServiceException.exit_code).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import ValidationError
from sqlalchemy import Engine

from core.config import KubeGuardianConfig, get_setting, load_kubeguardian_config
from core.db import create_db_engine
from core.error.service_exception import ErrorCode, ServiceException
from core.http import NamespacedHttp
from core.llm import build_chat_model
from service.agent.base import AgentResult
from service.checks.detectors.catalog import CATALOG
from service.pipeline.check import CheckOutcome, run_check
from service.pipeline.investigate import run_investigation
from service.sensors.collector import KubeReader

DEFAULT_DB = "kubeguardian.db"
VERDICT_LABEL = {
    "detected": "적발",
    "observing": "관찰 중",
    "passed": "통과",
    "unknown": "확인하지 못함",
}

ReaderFactory = Callable[[KubeGuardianConfig], KubeReader]
HttpFactory = Callable[[KubeGuardianConfig], Any]
LlmFactory = Callable[[str], BaseChatModel]
MAX_SYMPTOM_LENGTH = 500


def _http(config: KubeGuardianConfig) -> NamespacedHttp:
    return NamespacedHttp(config.target.namespace)


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


def check(args: argparse.Namespace, make_reader: ReaderFactory, make_http: HttpFactory) -> None:
    config = _load_config(args.config)
    reader = make_reader(config)
    engine = _open_db(args.db)
    print_outcome(run_check(reader, config, engine, trigger="check", http=make_http(config)))


def investigate(
    args: argparse.Namespace,
    make_reader: ReaderFactory,
    make_http: HttpFactory,
    make_llm: LlmFactory,
) -> None:
    symptom = args.symptom
    if symptom is not None and len(symptom) > MAX_SYMPTOM_LENGTH:
        raise ServiceException(
            ErrorCode.VALIDATION_ERROR, f"증상은 {MAX_SYMPTOM_LENGTH}자 이하로 쓴다"
        )
    config = _load_config(args.config)
    reader, http = make_reader(config), make_http(config)
    engine = _open_db(args.db)
    outcome = run_check(
        reader, config, engine, trigger="investigate", http=http, user_symptom=symptom
    )
    print_outcome(outcome)
    result = run_investigation(
        outcome,
        config,
        engine,
        run_id=f"kg-investigate-{outcome.run_id}",
        trigger="investigate",
        llm_factory=make_llm,
        k8s=reader,
        http=http,
        symptom=symptom,
    )
    print_investigation(result)


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
    if report.issues:
        top = min(report.issues, key=lambda i: i.rank)
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
            label = "성공" if sym.failed_step is None else f"실패({sym.severity})"
            print(f"  {label}  {sym.scenario}  {sym.summary}")
    gate = out.gate
    severity = f" ({gate.severity})" if gate.severity else ""
    print(f"\n게이트: 경로 {gate.path}{severity} — {gate.reason}")


def main(
    argv: Sequence[str] | None = None,
    *,
    make_reader: ReaderFactory = _k8s_reader,
    make_http: HttpFactory = _http,
    make_llm: LlmFactory = build_chat_model,
) -> int:
    parser = argparse.ArgumentParser(prog="kg", description="KubeGuardian CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="규칙 점검을 한 번 실행하고 결과를 출력한다")
    p_check.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_check.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    p_inv = sub.add_parser("investigate", help="점검 뒤 게이트가 열리면 조사 에이전트를 실행한다")
    p_inv.add_argument("symptom", nargs="?", help="사용자 증상 (선택, 최대 500자)")
    p_inv.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_inv.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(args, make_reader, make_http)
        elif args.command == "investigate":
            investigate(args, make_reader, make_http, make_llm)
    except ServiceException as e:
        print(
            f"오류 {e.error_code.code} {e.error_code.name}: {e.error_code.message}", file=sys.stderr
        )
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return e.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
