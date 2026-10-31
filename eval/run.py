"""평가 러너 최소판 (기획서 §9.3): 주입 → 증상 확인 → 반복 점검·게이트 → 채점 → 원복.

기준 조건(규칙만)으로 시나리오 하나를 돈다. --condition ai면 점검 뒤 게이트가 열렸을 때
조사 에이전트를 돌리고, 보고서 결론이 기대 1순위와 같은 원인인지 LLM으로 채점한다.
점검은 같은 DB로 간격(기본 300초)마다 반복하고, 기대 신호를 모두 적발하거나
상한(기본 2700초)에 닿으면 마지막 점검으로 채점한다. 기대 신호가 없는 시나리오는 한 번만 점검한다.
실행: uv run python eval/run.py <시나리오 ID> [--config kubeguardian.yaml]
      [--check-interval 300] [--check-max 2700] [--condition rules|ai] [--in-cluster]
종료 코드: 0 통과, 1 실패, 2 무효(증상이 나지 않음·원복 실패 등 실행 자체를 믿을 수 없음)
결과는 eval/history/<시각>-<ID>/에 result.json과 점검 DB로 남긴다 (커밋하지 않음).
"""

import argparse
import json
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import yaml  # noqa: E402

from core.accounts import load_accounts  # noqa: E402
from core.config import get_setting, load_kubeguardian_config  # noqa: E402
from core.db import create_db_engine  # noqa: E402
from core.guard.redact import redact_text  # noqa: E402
from core.http import NamespacedHttp  # noqa: E402
from core.k8s import K8sClient  # noqa: E402
from core.llm import build_chat_model  # noqa: E402
from core.tracing import setup_tracing  # noqa: E402
from judge import JUDGE_MODEL, JudgeResult, judge_top1  # noqa: E402
from remote import RemoteError, RemoteRunner  # noqa: E402
from scoring import coverage_record, expected_record, score  # noqa: E402
from service.agent.base import AgentResult  # noqa: E402
from service.pipeline.check import run_check  # noqa: E402
from service.pipeline.investigate import run_investigation  # noqa: E402

SCENARIOS = ROOT / "scenarios"
HISTORY = ROOT / "eval" / "history"
EXIT_PASS, EXIT_FAIL, EXIT_INVALID = 0, 1, 2


def sh(scenario: str, script: str) -> subprocess.CompletedProcess[str]:
    path = SCENARIOS / scenario / script
    return subprocess.run(
        ["bash", str(path)], capture_output=True, encoding="utf-8", errors="replace", cwd=ROOT
    )


def until_ok(scenario: str, script: str, timeout_s: int, interval_s: int = 10) -> tuple[bool, str]:
    """script가 성공할 때까지 반복한다 (롤아웃·엔드포인트 반영을 기다림)."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = sh(scenario, script)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 or time.monotonic() >= deadline:
            return r.returncode == 0, out
        time.sleep(interval_s)


def target_http(config: Any) -> NamespacedHttp:
    """시나리오용 테스트 계정(KG_ACCOUNTS_DIR)을 넣은 HTTP 클라이언트."""
    return NamespacedHttp(
        config.target.namespace, accounts=load_accounts(get_setting().KG_ACCOUNTS_DIR)
    )


def unknown_symptoms(outcome: Any) -> list[str]:
    """실행하지 못한 증상 시나리오 (이름: 사유). 하나라도 있으면 그 판정을 믿을 수 없다."""
    return [f"{x.scenario}: {x.summary}" for x in outcome.symptoms if x.status == "unknown"]


def log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def check_repeatedly(
    check: Callable[[], Any],
    expected: dict,
    *,
    interval_s: float,
    max_s: float,
    elapsed: Callable[[], float],
    sleep: Callable[[float], None] = time.sleep,
    on_check: Callable[[dict], None] = lambda _: None,
) -> tuple[Any, list[dict], dict[str, int]]:
    """기대 신호를 모두 적발하거나 상한에 닿을 때까지 interval_s마다 점검한다 (CronJob처럼).

    check는 같은 DB로 run_check를 부른다 (지속 시간 규칙이 앞 점검의 관찰 기록을 이어 본다).
    elapsed는 주입 시각부터 지난 초. 일정은 첫 점검 시작 시각 기준 interval_s 간격이고,
    다음 점검 시작이 max_s를 넘으면 멈춘다. 기대 신호가 없으면(H0·B·C 시나리오) 한 번만 점검한다.
    수집 실패나 실행하지 못한 증상 시나리오가 있으면 멈춘다 (무효 처리는 호출한 쪽이 한다).
    반환: (마지막 점검 결과, 점검별 기록, 기대 신호별 첫 적발 경과 초)
    """
    want = set(expected.get("must_signals") or [])
    checks: list[dict] = []
    first: dict[str, int] = {}
    first_start = elapsed()
    n = 0
    while True:
        outcome = check()
        t = round(elapsed())
        s = score(expected, outcome.signals, outcome.gate.path)
        for rule in sorted(want & set(s["detected"])):
            first.setdefault(rule, t)
        n += 1
        record = {
            "n": n,
            "elapsed_s": t,
            "gate_path": outcome.gate.path,
            "detected": s["detected"],
            "observing": s["observing"],
            "collect_failed": list(outcome.snapshot.failed),
        }
        checks.append(record)
        on_check(record)
        next_start = first_start + n * interval_s
        # 채점은 마지막 점검으로 하므로, 그 점검 하나에서 기대 신호가 모두 적발되어야 멈춘다
        if (
            outcome.snapshot.failed
            or unknown_symptoms(outcome)
            or want <= set(s["detected"])
            or next_start > max_s
        ):
            return outcome, checks, first
        sleep(max(0.0, next_start - elapsed()))


def investigate_and_judge(
    expected: dict,
    *,
    investigate: Callable[[], AgentResult | None],
    judge: Callable[[str], JudgeResult],
) -> tuple[dict, dict | None, list[dict]]:
    """조사를 돌리고 결론을 채점한다. 반환: (result.json의 ai 블록, 보고서 dict 또는 None, 단계).

    통과 = 조사가 보고서를 냈고(done·incomplete) 채점이 match이며 결론에 근거가 있다.
    채점이 match가 아니면 실패, 채점 자체가 실패(None)하면 무효(ai["invalid"])다.
    기대 게이트가 C면 조사하지 않아야 통과다. 조사가 돌았으면 채점 없이 실패한다.
    trace_id는 조사 전체를 감싸는 span의 id로 Phoenix에서 이 실행을 찾는 열쇠다 (추적 꺼짐: None).
    """
    from opentelemetry import trace

    setup_tracing()  # span을 열기 전에 켜야 span이 기록된다 (여러 번 불러도 한 번만 설정)
    with trace.get_tracer("eval").start_as_current_span("eval.investigation") as span:
        result = investigate()
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
    ai: dict = {
        "expected_top1": expected.get("expected_top1"),
        "trace_id": trace_id,
        "passed": False,
    }
    expect_closed = expected.get("gate_path") == "C"
    if result is None:
        ai["status"] = "skipped"  # 게이트 경로 C: 조사하지 않음
        ai["passed"] = expect_closed
        return ai, None, []
    steps = [s.model_dump(mode="json") for s in result.steps]
    ai["status"] = result.status
    ai["usage"] = result.usage.model_dump()
    if result.error:
        ai["error"] = result.error
    if expect_closed:
        ai["fail_reason"] = "기대 게이트 C인데 조사가 실행됐다"
        return ai, None, steps
    if result.report is None:
        return ai, None, steps
    ai["conclusion"] = result.report.conclusion
    report_dict = result.report.model_dump(mode="json")
    ai["must_check"] = coverage_record(expected.get("must_check") or {}, report_dict["coverage"])
    top = min(result.report.issues, key=lambda i: i.rank, default=None)
    ai["cause_resource"] = top.cause_resource.model_dump() if top and top.cause_resource else None
    verdict = judge(result.report.conclusion)
    ai["judge"] = {"match": verdict.match, "reason": verdict.reason}
    ai["passed"] = verdict.match is True
    if verdict.match is None:
        ai["invalid"] = verdict.reason or "채점 실패"
    ai["conclusion_supported"] = result.report.conclusion_supported
    validate = next((s for s in result.steps if s.node == "validate"), None)
    ai["demoted"] = list(validate.detail.get("demoted", [])) if validate else []
    if ai["passed"] and not ai["conclusion_supported"]:
        # 근거 없는 결론이 우연히 맞은 것은 통과가 아니다
        ai["passed"] = False
        ai["fail_reason"] = "결론이 인용한 근거가 없다 (conclusion_supported=False)"
    return ai, report_dict, steps


def save_ai_artifacts(out_dir: Path, report: dict | None, steps: list[dict]) -> None:
    """보고서와 단계 기록을 history 디렉터리에 남긴다 (도구 결과는 이미 마스킹된 상태)."""
    for name, data in (("report.json", report), ("steps.json", steps or None)):
        if data is not None:
            (out_dir / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def decide_exit(rules_passed: bool, ai: dict) -> int:
    """점검 채점과 AI 조사 결과를 종료 코드로 합친다. 채점 실패는 실패가 아니라 무효다."""
    if ai.get("invalid"):
        return EXIT_INVALID
    return EXIT_PASS if rules_passed and ai["passed"] else EXIT_FAIL


def run(
    scenario: str,
    config_path: str,
    symptom_timeout_s: int,
    interval_s: int,
    max_s: int,
    condition: str = "rules",
    in_cluster: bool = False,
) -> int:
    expected = yaml.safe_load((SCENARIOS / scenario / "expected.yaml").read_text(encoding="utf-8"))
    config = load_kubeguardian_config(config_path)
    started = datetime.now(UTC)
    out_dir = HISTORY / f"{started:%Y%m%dT%H%M%SZ}-{scenario}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "scenario": scenario,
        "condition": "ai" if condition == "ai" else "rules-only",
        "started_at": started.isoformat(),
        "expected": expected_record(expected),
    }
    exit_code = EXIT_INVALID

    # --in-cluster: 점검·조사는 runner Pod에서 한다. 주입 전에 Pod부터 확인한다 (없으면 주입 안 함)
    remote: RemoteRunner | None = None
    if in_cluster:
        remote = RemoteRunner(out_dir.name)
        try:
            pod = remote.start()
            result["in_cluster"] = {"pod": pod, **remote.describe()}
        except RemoteError as e:
            result["invalid"] = f"runner Pod 확인 실패: {e}"
            return finish(result, out_dir, exit_code)

    # 주입 전에 정상 기준선을 확인한다. 이미 깨져 있으면 증상 확인이 다른 원인으로 통과할 수 있다
    ok, out = until_ok("H0", "verify_symptom.sh", 60)
    log("baseline", out)
    if not ok:
        result["invalid"] = "주입 전 정상 기준선(H0) 확인 실패"
        return finish(result, out_dir, exit_code)

    check_started = False  # 점검을 한 번이라도 시작했을 때만 Pod의 DB를 가져온다
    try:
        injected_at = time.monotonic()
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

        # 같은 DB로 반복 점검한다. 지속 시간 규칙(W02 등)은 앞 점검의 관찰 기록이 있어야 적발된다
        if remote is None:
            reader = K8sClient.from_config(config)
            engine = create_db_engine(str(out_dir / "kg.db"))
        result["check_interval_s"] = interval_s
        result["check_max_s"] = max_s
        result["checks"] = []
        check_started = True

        def on_check(c: dict) -> None:
            # 바로 쌓아 둔다. 반복 중 예외가 나도 그때까지의 점검이 result.json에 남는다
            result["checks"].append(c)
            log(
                f"check {c['n']}",
                f"{c['elapsed_s']}s 경로 {c['gate_path']} 적발 {c['detected']}"
                f" 관찰 중 {c['observing']}",
            )

        outcome, checks, first = check_repeatedly(
            remote.check
            if remote
            else lambda: run_check(
                reader,
                config,
                engine,
                trigger="check",
                http=target_http(config),
            ),
            expected,
            interval_s=interval_s,
            max_s=max_s,
            elapsed=lambda: time.monotonic() - injected_at,
            on_check=on_check,
        )
        result["first_detected_s"] = {
            r: first.get(r) for r in sorted(expected.get("must_signals") or [])
        }
        result["check_run_id"] = outcome.run_id
        result["collect_failed"] = outcome.snapshot.failed
        if outcome.snapshot.failed:
            # 수집이 실패하면 규칙이 "확인하지 못함"이 되고 게이트가 C로 가서 H0이 통과처럼 보인다
            result["invalid"] = f"수집 실패: {', '.join(outcome.snapshot.failed)}"
            return exit_code
        unknown = unknown_symptoms(outcome)
        if unknown:
            # 시나리오를 실행하지 못하면 증상 유무를 모른다. 게이트가 C로 가서 통과처럼 보인다
            result["unknown_symptoms"] = unknown
            result["invalid"] = f"증상 감지 확인하지 못함: {'; '.join(unknown)}"
            return exit_code
        result["score"] = score(expected, outcome.signals, outcome.gate.path)
        s = result["score"]
        log(
            "check",
            f"점검 {len(checks)}회, 마지막 게이트 경로 {s['gate_path']} (기대 {s['gate_expected']})"
            f" — {outcome.gate.reason}. 첫 적발(주입 기준 초) {result['first_detected_s']}",
        )
        log(
            "score",
            f"기대 {s['expected']} 적발 {s['detected']} 놓침 {s['missed']}"
            f" 금지 적발 {s['forbidden_hit']} 기대 밖 {s['unexpected']}"
            f" 관찰 중 {s['observing']} 확인하지 못함 {s['unknown']}",
        )
        passed = s["passed"]
        if condition == "ai":
            run_id = f"eval-{scenario}-{started:%Y%m%dT%H%M%S}"
            remote_trace: dict = {}

            def investigate_remote() -> AgentResult | None:
                res, remote_trace["id"] = remote.investigate(outcome.run_id, run_id)
                return res

            ai, report, steps = investigate_and_judge(
                expected,
                investigate=investigate_remote
                if remote
                else lambda: run_investigation(
                    outcome,
                    config,
                    engine,
                    run_id=run_id,
                    k8s=reader,
                    http=target_http(config),
                ),
                judge=lambda conclusion: judge_top1(
                    conclusion, expected, build_chat_model(JUDGE_MODEL)
                ),
            )
            if remote:
                ai["trace_id"] = remote_trace.get("id")  # Pod 안 추적의 id (Mac의 span이 아니다)
            result["ai"] = ai
            save_ai_artifacts(out_dir, report, steps)
            log(
                "ai",
                f"{ai['status']} 결론 {ai.get('conclusion')!r} | 기대 {ai['expected_top1']!r}"
                f" | 채점 {ai.get('judge')} | 사용 {ai.get('usage')} | trace {ai['trace_id']}",
            )
            if ai.get("invalid"):
                result["invalid"] = ai["invalid"]
            exit_code = decide_exit(passed, ai)
        else:
            exit_code = EXIT_PASS if passed else EXIT_FAIL
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
        # 원복을 먼저 끝낸 뒤 DB를 가져온다. 가져오기가 어떻게 실패해도 원복은 이미 끝나 있다
        if remote and check_started:
            try:
                remote.fetch(out_dir)
            except Exception as e:
                result["db_fetch_failed"] = redact_text(f"{type(e).__name__}: {e}")
                log("fetch", f"점검 DB를 가져오지 못함: {result['db_fetch_failed']}")
            if remote.db_incomplete:
                result["db_incomplete"] = True
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
    p = argparse.ArgumentParser(description="KubeGuardian 평가 러너")
    p.add_argument("scenario", help="scenarios/<ID>")
    p.add_argument("--config", default="kubeguardian.yaml")
    p.add_argument("--symptom-timeout", type=int, default=180, help="증상 확인 대기 (초)")
    p.add_argument("--check-interval", type=int, default=300, help="반복 점검 간격 (초)")
    p.add_argument(
        "--check-max", type=int, default=2700, help="반복 점검 상한, 주입 시각 기준 (초)"
    )
    p.add_argument(
        "--in-cluster",
        action="store_true",
        help="점검·조사를 kubeguardian 네임스페이스의 runner Pod에서 실행 (make kg-up 필요)",
    )
    p.add_argument(
        "--condition", choices=["rules", "ai"], default="rules", help="rules: 규칙만, ai: 조사 포함"
    )
    args = p.parse_args()
    if args.check_interval <= 0:
        p.error("--check-interval은 0보다 커야 한다")
    if not (SCENARIOS / args.scenario / "expected.yaml").is_file():
        print(f"시나리오 없음: scenarios/{args.scenario}/expected.yaml", file=sys.stderr)
        return EXIT_INVALID
    try:
        return run(
            args.scenario,
            args.config,
            args.symptom_timeout,
            args.check_interval,
            args.check_max,
            args.condition,
            args.in_cluster,
        )
    except Exception as e:  # 설정 파일 오류 등 주입 전에 난 오류
        print(f"무효: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
