"""runner Pod 안 진입점: 점검·조사를 한 번 실행하고 결과를 stdout 한 줄 JSON으로 낸다.

평가 러너(Mac)가 `kubectl exec <pod> -- python -m runner_step <명령> ... --json`으로 부른다.
경고·진단은 stderr로만 나가고, 비밀값은 출력하지 않는다. 수집기(상류)가 redact_mapping으로 가린 값을
전제로, 출력 직전에 문자열 값만 redact_text로 한 번 더 거른다 (키 이름으로는 가리지 않는다).

  check        --db <경로> [--config ...]
  investigate  --db <경로> --check-run-id <N> --run-id <ID> [--config ...]

check는 같은 DB로 반복해서 부른다 (지속 시간 규칙이 앞 점검의 관찰 기록을 이어 본다).
조사는 점검 때 DB 옆에 남긴 outcome-<run_id>.json으로 그 점검 결과를 되살려 쓴다.
종료 코드: 0 성공, 1 서버 오류, 2 입력 오류 (ServiceException.exit_code).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from core.config import KubeGuardianConfig, get_setting, load_kubeguardian_config
from core.db import create_db_engine
from core.error.service_exception import ErrorCode, ServiceException
from core.guard.redact import redact_text
from service.checks.signal_board import build_signal_board
from service.model import ClusterSnapshot, GateResult, Signal
from service.pipeline.check import CheckOutcome, run_check
from service.sensors.collector import KubeReader

ReaderFactory = Callable[[KubeGuardianConfig], KubeReader]


def _k8s_reader(config: KubeGuardianConfig) -> KubeReader:
    from core.k8s import K8sClient

    return K8sClient.from_config(config)


def scrub(data: Any) -> Any:
    """문자열 값만 마스킹한 새 객체. 키 이름으로는 가리지 않는다 (kind `Secret` 같은 키 보존).

    수집기가 이미 마스킹한 값을 한 번 더 거르는 방어선이다 (멱등).
    """
    if isinstance(data, dict):
        return {k: scrub(v) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return redact_text(data) if isinstance(data, str) else data


def outcome_file(db: Path, run_id: int) -> Path:
    return db.parent / f"outcome-{run_id}.json"


def _engine(db: Path) -> Engine:
    db.parent.mkdir(parents=True, exist_ok=True)
    return create_db_engine(db)


def check_payload(out: CheckOutcome) -> dict:
    """평가 러너가 받는 점검 결과. 채점에 필요한 것만 담는다 (스냅샷 전체는 outcome 파일에만)."""
    return {
        "run_id": out.run_id,
        "gate_path": out.gate.path,
        "gate": out.gate.model_dump(mode="json"),
        "signals": [s.model_dump(mode="json") for s in out.signals],
        "snapshot_failed": dict(out.snapshot.failed),
    }


def load_outcome(db: Path, run_id: int) -> CheckOutcome:
    path = outcome_file(db, run_id)
    if not path.is_file():
        raise ServiceException(ErrorCode.BAD_REQUEST, f"저장된 outcome 없음: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    signals = [Signal.model_validate(s) for s in data["signals"]]
    return CheckOutcome(
        run_id=data["run_id"],
        snapshot=ClusterSnapshot.model_validate(data["snapshot"]),
        signals=signals,
        gate=GateResult.model_validate(data["gate"]),
        board=build_signal_board(signals),
    )


def do_check(args: argparse.Namespace, make_reader: ReaderFactory) -> dict:
    config = load_kubeguardian_config(args.config)
    out = run_check(make_reader(config), config, _engine(Path(args.db)), trigger="check")
    full = check_payload(out) | {"snapshot": out.snapshot.model_dump(mode="json")}
    outcome_file(Path(args.db), out.run_id).write_text(
        json.dumps(scrub(full), ensure_ascii=False), encoding="utf-8"
    )
    return check_payload(out)


def do_investigate(
    args: argparse.Namespace, make_reader: ReaderFactory, llm_factory: Callable | None
) -> dict:
    from opentelemetry import trace

    from core.http import NamespacedHttp
    from core.llm import build_chat_model
    from core.tracing import setup_tracing
    from service.pipeline.investigate import run_investigation

    config = load_kubeguardian_config(args.config)
    db = Path(args.db)
    out = load_outcome(db, args.check_run_id)
    setup_tracing()  # span을 열기 전에 켜야 span이 기록된다
    with trace.get_tracer("eval").start_as_current_span("eval.investigation") as span:
        result = run_investigation(
            out,
            config,
            _engine(db),
            run_id=args.run_id,
            llm_factory=llm_factory or build_chat_model,
            k8s=make_reader(config),
            http=NamespacedHttp(config.target.namespace),
        )
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
    if result is None:
        return {"skipped": True}
    return result.model_dump(mode="json") | {"trace_id": trace_id}


def main(
    argv: Sequence[str] | None = None,
    *,
    make_reader: ReaderFactory = _k8s_reader,
    llm_factory: Callable | None = None,
) -> int:
    p = argparse.ArgumentParser(prog="runner_step", description="runner Pod 안 점검·조사 한 단계")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("check", "investigate"):
        s = sub.add_parser(name)
        s.add_argument("--config", default=get_setting().KG_CONFIG_PATH)
        s.add_argument("--db", required=True)
        s.add_argument("--json", action="store_true", required=True, help="결과를 한 줄 JSON으로")
        if name == "investigate":
            s.add_argument("--check-run-id", type=int, required=True)
            s.add_argument("--run-id", required=True)
    args = p.parse_args(argv)
    try:
        if args.command == "check":
            payload = do_check(args, make_reader)
        else:
            payload = do_investigate(args, make_reader, llm_factory)
    except ServiceException as e:
        print(f"오류 {e.error_code.code} {e.error_code.name}: {e.detail}", file=sys.stderr)
        return e.exit_code
    print(json.dumps(scrub(payload), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
