"""KubeGuardian CLI (개발·평가·CronJob 진입). 실행: uv run python app/cli.py check

kg check: 규칙 점검을 한 번 돌리고 규칙별 판정 결과·판정에 쓴 값·게이트 경로를 출력한다.
종료 코드: 0 점검 완료(게이트 경로와 무관), 2 입력 오류, 1 서버 오류 (ServiceException.exit_code).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from core.config import KubeGuardianConfig, get_setting, load_kubeguardian_config
from core.db import create_db_engine
from core.error.service_exception import ErrorCode, ServiceException
from service.checks.detectors.catalog import CATALOG
from service.pipeline.check import CheckOutcome, run_check
from service.sensors.collector import KubeReader

DEFAULT_DB = "kubeguardian.db"
VERDICT_LABEL = {
    "detected": "적발",
    "observing": "관찰 중",
    "passed": "통과",
    "unknown": "확인하지 못함",
}

ReaderFactory = Callable[[KubeGuardianConfig], KubeReader]


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


def check(args: argparse.Namespace, make_reader: ReaderFactory) -> None:
    config = _load_config(args.config)
    reader = make_reader(config)
    try:
        engine = create_db_engine(args.db)
    except Exception as e:
        raise ServiceException(ErrorCode.STORAGE_ERROR, type(e).__name__) from e
    print_outcome(run_check(reader, config, engine, trigger="check"))


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
    gate = out.gate
    severity = f" ({gate.severity})" if gate.severity else ""
    print(f"\n게이트: 경로 {gate.path}{severity} — {gate.reason}")


def main(argv: Sequence[str] | None = None, *, make_reader: ReaderFactory = _k8s_reader) -> int:
    parser = argparse.ArgumentParser(prog="kg", description="KubeGuardian CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="규칙 점검을 한 번 실행하고 결과를 출력한다")
    p_check.add_argument("--config", default=get_setting().KG_CONFIG_PATH, help="kubeguardian.yaml")
    p_check.add_argument("--db", default=DEFAULT_DB, help="SQLite 파일 (기본: kubeguardian.db)")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(args, make_reader)
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
