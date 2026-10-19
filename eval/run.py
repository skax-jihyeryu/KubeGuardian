"""평가 러너 최소판 (기획서 §9.3): 주입 → 증상 확인 → 점검·게이트 → 채점 → 원복.

기준 조건(규칙만)으로 시나리오 하나를 돈다. AI 조사 단계는 W2에 연결한다.
실행: uv run python eval/run.py <시나리오 ID> [--config kubeguardian.yaml]
종료 코드: 0 통과, 1 실패, 2 무효(증상이 나지 않음·원복 실패 등 실행 자체를 믿을 수 없음)
결과는 eval/history/<시각>-<ID>/에 result.json과 점검 DB로 남긴다 (커밋하지 않음).
"""

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import yaml  # noqa: E402

from core.config import load_kubeguardian_config  # noqa: E402
from core.db import create_db_engine  # noqa: E402
from core.k8s import K8sClient  # noqa: E402
from scoring import score  # noqa: E402
from service.pipeline.check import run_check  # noqa: E402

SCENARIOS = ROOT / "scenarios"
HISTORY = ROOT / "eval" / "history"
EXIT_PASS, EXIT_FAIL, EXIT_INVALID = 0, 1, 2


def sh(scenario: str, script: str) -> subprocess.CompletedProcess[str]:
    path = SCENARIOS / scenario / script
    return subprocess.run(["bash", str(path)], capture_output=True, text=True, cwd=ROOT)


def until_ok(scenario: str, script: str, timeout_s: int, interval_s: int = 10) -> tuple[bool, str]:
    """script가 성공할 때까지 반복한다 (롤아웃·엔드포인트 반영을 기다림)."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = sh(scenario, script)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 or time.monotonic() >= deadline:
            return r.returncode == 0, out
        time.sleep(interval_s)


def log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def run(scenario: str, config_path: str, symptom_timeout_s: int) -> int:
    expected = yaml.safe_load((SCENARIOS / scenario / "expected.yaml").read_text(encoding="utf-8"))
    config = load_kubeguardian_config(config_path)
    started = datetime.now(UTC)
    out_dir = HISTORY / f"{started:%Y%m%dT%H%M%SZ}-{scenario}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "scenario": scenario,
        "condition": "rules-only",
        "started_at": started.isoformat(),
    }
    exit_code = EXIT_INVALID

    # 주입 전에 정상 기준선을 확인한다. 이미 깨져 있으면 증상 확인이 다른 원인으로 통과할 수 있다
    ok, out = until_ok("H0", "verify_symptom.sh", 60)
    log("baseline", out)
    if not ok:
        result["invalid"] = "주입 전 정상 기준선(H0) 확인 실패"
        return finish(result, out_dir, exit_code)

    try:
        r = sh(scenario, "inject.sh")
        log("inject", (r.stdout + r.stderr).strip())
        if r.returncode != 0:
            result["invalid"] = "inject 실패"
            return exit_code

        ok, out = until_ok(scenario, "verify_symptom.sh", symptom_timeout_s)
        log("symptom", out)
        if not ok:
            result["invalid"] = "증상이 나지 않음"
            return exit_code

        outcome = run_check(
            K8sClient.from_config(config),
            config,
            create_db_engine(str(out_dir / "kg.db")),
            trigger="check",
        )
        result["check_run_id"] = outcome.run_id
        result["collect_failed"] = outcome.snapshot.failed
        if outcome.snapshot.failed:
            # 수집이 실패하면 규칙이 "확인하지 못함"이 되고 게이트가 C로 가서 H0이 통과처럼 보인다
            result["invalid"] = f"수집 실패: {', '.join(outcome.snapshot.failed)}"
            return exit_code
        result["score"] = score(expected, outcome.signals, outcome.gate.path)
        s = result["score"]
        log(
            "check",
            f"게이트 경로 {s['gate_path']} (기대 {s['gate_expected']}) — {outcome.gate.reason}",
        )
        log(
            "score",
            f"기대 {s['expected']} 적발 {s['detected']} 놓침 {s['missed']}"
            f" 기대 밖 {s['unexpected']} 관찰 중 {s['observing']} 확인하지 못함 {s['unknown']}",
        )
        exit_code = EXIT_PASS if s["passed"] else EXIT_FAIL
    except Exception as e:  # 설정·kubeconfig·저장소 오류 등: 실행 자체를 믿을 수 없다
        traceback.print_exc()
        result["invalid"] = f"{type(e).__name__}: {e}"
        exit_code = EXIT_INVALID
    finally:
        r = sh(scenario, "reset.sh")
        log("reset", (r.stdout + r.stderr).strip())
        recovered = r.returncode == 0
        if recovered and scenario != "H0":
            recovered, out = until_ok("H0", "verify_symptom.sh", 180)
            log("recover", out)
        result["recovered"] = recovered
        if not recovered:
            result["invalid"] = result.get("invalid") or "원복 확인 실패"
            exit_code = EXIT_INVALID
        exit_code = finish(result, out_dir, exit_code)
    return exit_code


def finish(result: dict, out_dir: Path, exit_code: int) -> int:
    result["finished_at"] = datetime.now(UTC).isoformat()
    result["exit_code"] = exit_code
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    verdict = {EXIT_PASS: "통과", EXIT_FAIL: "실패", EXIT_INVALID: "무효"}[exit_code]
    reason = f" — {result['invalid']}" if result.get("invalid") else ""
    log(
        "result",
        f"{result['scenario']} {verdict}{reason}  ({out_dir.relative_to(ROOT)}/result.json)",
    )
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="KubeGuardian 평가 러너 (기준 조건: 규칙만)")
    p.add_argument("scenario", help="scenarios/<ID>")
    p.add_argument("--config", default="kubeguardian.yaml")
    p.add_argument("--symptom-timeout", type=int, default=180, help="증상 확인 대기 (초)")
    args = p.parse_args()
    if not (SCENARIOS / args.scenario / "expected.yaml").is_file():
        print(f"시나리오 없음: scenarios/{args.scenario}/expected.yaml", file=sys.stderr)
        return EXIT_INVALID
    try:
        return run(args.scenario, args.config, args.symptom_timeout)
    except Exception as e:  # 설정 파일 오류 등 주입 전에 난 오류
        print(f"무효: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
