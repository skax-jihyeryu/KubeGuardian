"""묶음 집계 리포트 (spec §5.2·§6·§7): history의 같은 batch 결과를 모아 Markdown으로 낸다.

실행: uv run python eval/report.py <batch> [--baseline <batch>] [--out eval/report.md]
사람 판정 라벨(eval/labels/<디렉터리 이름>.yaml)이 있는 회차는 judge 값을 라벨로 덮어 다시 분류한다.
"""

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "eval"))

from judge import ClaimVerdict, support_summary  # noqa: E402
from scoring import classify  # noqa: E402

HISTORY = ROOT / "eval" / "history"
LABELS = ROOT / "eval" / "labels"
SCENARIOS = ROOT / "scenarios"
AGREEMENT_MIN = 0.9
REGRESSION_DROP = 2


def load_batch(history: Path, batch: str) -> list[tuple[str, dict]]:
    out = []
    for path in sorted(history.glob("*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("batch") == batch:
            out.append((path.parent.name, result))
    return out


def load_labels(labels_dir: Path) -> dict[tuple[str, int], dict]:
    labels: dict[tuple[str, int], dict] = {}
    if not labels_dir.is_dir():
        return labels
    for path in sorted(labels_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in data.get("runs") or []:
            labels[(path.stem, int(item["index"]))] = item
    return labels


def apply_label(run: dict, label: dict | None) -> dict:
    """라벨이 있으면 judge_match와 주장 판정을 덮어쓰고 다시 분류한 사본을 돌려준다."""
    out = json.loads(json.dumps(run))
    if label is None or out.get("failure") in (None, "F0", "error"):
        return out
    out["orig_judge_match"] = out.get("judge_match")
    out["orig_claims"] = out.get("claims") or []
    judge_match = bool(label["top1_match"])
    given = {c["source"]: c["verdict"] for c in label.get("claims") or []}
    claims = [
        {**c, "verdict": given.get(c["source"], c["verdict"])} for c in out.get("claims") or []
    ]
    summary = support_summary([ClaimVerdict(**c) for c in claims])
    ev = out.get("eval") or {}
    key = {k: ev.get(k) for k in ("key_found", "key_missing", "key_all_found", "tool_limited")}
    failure = classify(
        cause_match=ev.get("cause_match", False),
        judge_match=judge_match,
        forbidden_cause=ev.get("forbidden_cause", False),
        key=key,
        conclusion_support=summary["conclusion_support"],
    )
    out.update(
        judge_match=judge_match,
        claims=claims,
        support_rate=summary["support_rate"],
        conclusion_support=summary["conclusion_support"],
        failure=failure,
        passed=failure == "pass",
        labeled=True,
    )
    out["eval"] = {**ev, "judge_match": judge_match}
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def _rate(k: int, n: int) -> float | None:
    return k / n if n else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _scenario_info(scenarios_dir: Path, sid: str) -> dict:
    path = scenarios_dir / sid / "expected.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {k: (data or {}).get(k) for k in ("level", "k8s_visible", "gate_path")}


def _tokens(run: dict) -> int:
    u = run.get("usage") or {}
    return (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def _num(v: float | None, fmt: str = "{:.0f}") -> str:
    return "-" if v is None else fmt.format(v)


def _ci(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({lo:.0%}~{hi:.0%})" if n else "0/0"


def summarize(results: list[tuple[str, dict]], labels: dict, scenarios_dir: Path) -> dict:
    scen: dict[str, dict] = {}
    rules: dict[str, list[bool]] = defaultdict(list)
    rules_invalid: dict[str, int] = defaultdict(int)
    agree_conc, agree_claims = [], []
    for name, result in results:
        sid = result["scenario"]
        if result.get("condition") == "rules-only":
            if result.get("invalid"):
                rules_invalid[sid] += 1
            else:
                rules[sid].append(bool((result.get("score") or {}).get("passed")))
            continue
        s = scen.setdefault(
            sid,
            {
                **_scenario_info(scenarios_dir, sid),
                "runs": [],
                "invalid": 0,
                "errors": 0,
                "total": 0,
            },
        )
        ai_runs = result.get("ai_runs") or []
        if result.get("invalid") and not ai_runs:
            s["total"] += 1
            s["invalid"] += 1  # 주입·증상 실패 등으로 회차가 없어도 사라지지 않게 센다
        rules_ok = (result.get("score") or {}).get("passed") is not False
        for i, run in enumerate(ai_runs):  # 라벨 index는 0부터 (ai_runs 위치 = report-<i>.json)
            s["total"] += 1
            if result.get("invalid") or run.get("invalid") or run.get("failure") == "F0":
                s["invalid"] += 1
                continue
            if run.get("failure") == "error":
                s["errors"] += 1
            run = apply_label(run, labels.get((name, i)))
            if run.get("labeled"):
                agree_conc.append(run["orig_judge_match"] == run["judge_match"])
                mine = {c["source"]: c["verdict"] for c in run["claims"]}
                labeled = {c["source"] for c in labels[(name, i)].get("claims") or []}
                agree_claims += [
                    c["verdict"] == mine[c["source"]]
                    for c in run["orig_claims"]
                    if c["source"] in labeled
                ]
            run["rules_failed"] = not rules_ok  # AI 통과는 규칙 채점 통과를 전제한다 (spec §4)
            s["runs"].append(run)
    for s in scen.values():
        runs = s.pop("runs")
        s["runs"] = runs
        s["valid"] = len(runs)
        s["passed"] = sum(_passed(r) for r in runs)
        s["rules_fail"] = sum(bool(r.get("rules_failed")) for r in runs)
        s["dist"] = dict(sorted(_count(_outcome(r) for r in runs).items()))
        ran = [r for r in runs if _outcome(r) not in NOT_INVESTIGATED]  # 조사가 돈 회차
        judged_runs = [r for r in ran if r.get("eval")]
        for key in ("cause_match", "judge_match", "key_all_found"):
            s[key] = _rate(
                sum(bool((r.get("eval") or {}).get(key)) for r in judged_runs), len(judged_runs)
            )
        s["support_rate"] = _mean(
            [r["support_rate"] for r in ran if r.get("support_rate") is not None]
        )
        s["seconds"] = _median(
            [
                (r.get("usage") or {}).get("seconds")
                for r in ran
                if (r.get("usage") or {}).get("seconds") is not None
            ]
        )
        s["tokens"] = _median([_tokens(r) for r in ran])
        s["limited_failures"] = sum(bool(r.get("limited")) and not _passed(r) for r in runs)
        s["consistent"] = len({_outcome(r) for r in runs}) == 1 if runs else None
    # KPI는 조사가 돈 회차만 센다: skip(정상 생략)·not_run은 뺀다. gate_fail은 남긴다
    all_runs = [r for s in scen.values() for r in s["runs"] if _outcome(r) not in NOT_INVESTIGATED]
    invisible = [
        r
        for s in scen.values()
        if s["k8s_visible"] is False
        for r in s["runs"]
        if _outcome(r) not in NOT_INVESTIGATED
    ]

    def both(rs: list[dict]) -> float | None:
        ok = sum(
            bool(r["eval"].get("cause_match") and r["judge_match"]) for r in rs if r.get("eval")
        )
        return _rate(ok, len(rs))

    def group(attr: str) -> dict:
        g: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for s in scen.values():
            g[str(s[attr])][0] += s["passed"]
            g[str(s[attr])][1] += s["valid"]
        return dict(sorted(g.items()))

    judged = [s for s in scen.values() if s["consistent"] is not None]
    n_conc, n_claims = len(agree_conc), len(agree_claims)
    conc = _rate(sum(agree_conc), n_conc)
    claims = _rate(sum(agree_claims), n_claims)
    return {
        "scenarios": dict(sorted(scen.items())),
        "by_level": group("level"),
        "by_path": group("gate_path"),
        "consistency": _rate(sum(s["consistent"] for s in judged), len(judged)),
        "kpi": {
            "accuracy": both(all_runs),
            "coverage": both(invisible),
            "support_rate": _mean(
                [r["support_rate"] for r in all_runs if r.get("support_rate") is not None]
            ),
            "seconds": _median(
                [
                    (r.get("usage") or {}).get("seconds")
                    for r in all_runs
                    if (r.get("usage") or {}).get("seconds") is not None
                ]
            ),
            "tokens": _median([_tokens(r) for r in all_runs]),
        },
        "judge_agreement": {
            "conclusion": conc,
            "claims": claims,
            "n_conclusion": n_conc,
            "n_claims": n_claims,
            "low": any(v is not None and v < AGREEMENT_MIN for v in (conc, claims)),
        },
        "rules": {
            sid: (sum(rules[sid]), len(rules[sid]), rules_invalid[sid])
            for sid in sorted(set(rules) | set(rules_invalid))
        },
    }


PASSING = ("pass", "skip")
NOT_INVESTIGATED = ("skip", "not_run")  # 조사가 돌지 않은 회차 (KPI 분모에서 뺀다)


def _outcome(run: dict) -> str:
    """분류. 게이트 C 정상 생략은 skip(통과), 기대 C인데 조사가 열렸으면 gate_fail."""
    if run.get("failure"):
        return run["failure"]
    if run.get("passed"):
        return "skip"
    return "gate_fail" if run.get("fail_reason") else "not_run"


def _passed(run: dict) -> bool:
    """AI 조건 통과 = 규칙 채점 통과 + 회차 분류가 통과 (spec §4)."""
    return _outcome(run) in PASSING and not run.get("rules_failed")


def _dist_change(c: dict | None, b: dict | None) -> str:
    cd, bd = (c or {}).get("dist", {}), (b or {}).get("dist", {})
    parts = [
        f"{k} {bd.get(k, 0)}→{cd.get(k, 0)}"
        for k in sorted(set(cd) | set(bd))
        if cd.get(k, 0) != bd.get(k, 0)
    ]
    return ", ".join(parts) or "-"


def _count(items) -> dict:
    out: dict = defaultdict(int)
    for i in items:
        out[i] += 1
    return out


def _regression_rows(summary: dict, baseline: dict) -> tuple[list[str], int, int]:
    rows, cur_l1, base_l1 = [], 0, 0
    for sid in sorted(set(summary["scenarios"]) | set(baseline["scenarios"])):
        c, b = summary["scenarios"].get(sid), baseline["scenarios"].get(sid)
        cp, bp = (c["passed"] if c else 0), (b["passed"] if b else 0)
        level = (c or b)["level"]
        if level == "L1":
            cur_l1, base_l1 = cur_l1 + cp, base_l1 + bp
        flag = "회귀" if b and c and bp - cp >= REGRESSION_DROP else ""
        rows.append(f"| {sid} | {bp} → {cp} | {_dist_change(c, b)} | {flag} |")
    return rows, cur_l1, base_l1


def render(summary: dict, baseline: dict | None) -> str:
    out = ["# 평가 리포트", ""]
    out += [
        "## 시나리오별",
        "",
        "| 시나리오 | 통과 | 분류 | cause | judge | 핵심근거 | 근거일치 "
        "| 시간(s) | 토큰 | 무효 | error | 상한 실패 | 규칙 실패 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sid, s in summary["scenarios"].items():
        dist = ", ".join(f"{k}:{v}" for k, v in s["dist"].items()) or "-"
        out.append(
            f"| {sid} | {s['passed']}/{s['valid']} | {dist} | {_pct(s['cause_match'])} | "
            f"{_pct(s['judge_match'])} | {_pct(s['key_all_found'])} | "
            f"{_num(s['support_rate'], '{:.2f}')} | "
            f"{_num(s['seconds'])} | {_num(s['tokens'])} | {s['invalid']} | {s['errors']} | "
            f"{s['limited_failures']} | {s['rules_fail']} |"
        )
    out += [
        "",
        "무효(F0) 회차는 분모에서 빼고, error는 분모에 넣는다. "
        "통과 = 규칙 채점 통과 + 회차 분류 pass/skip이고, 분류 분포는 에이전트 분류 그대로다. "
        "규칙 실패는 규칙 채점이 실패한 회차 수. 상한 실패는 `limited`로 끝난 실패 회차 수.",
        "cause·judge·핵심근거·근거일치·시간·토큰은 조사가 돈 유효 회차(skip·not_run 제외, "
        "gate_fail 포함)가 분모다.",
        "",
    ]
    for title, key in (
        ("레벨별 통과율 (Wilson 95%)", "by_level"),
        ("경로별 통과율 (Wilson 95%)", "by_path"),
    ):
        out += [f"## {title}", "", "| 구분 | 통과 |", "|---|---|"]
        out += [f"| {k} | {_ci(*v)} |" for k, v in summary[key].items()]
        out.append("")
    out += [f"일관성(모든 유효 회차가 같은 분류인 시나리오): {_pct(summary['consistency'])}", ""]
    k = summary["kpi"]
    out += [
        "## KPI",
        "",
        "분모: 조사가 돈 유효 회차 (skip·not_run 제외, gate_fail 포함). "
        "커버리지는 그중 k8s_visible: false 시나리오.",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 원인 정확도 | {_pct(k['accuracy'])} |",
        f"| 원인 규명 커버리지 (k8s_visible: false) | {_pct(k['coverage'])} |",
        f"| 근거 일치율 | {_num(k['support_rate'], '{:.2f}')} |",
        f"| 조사 시간 중앙값(s) | {_num(k['seconds'])} |",
        f"| 토큰 중앙값 | {_num(k['tokens'])} |",
        "",
    ]
    a = summary["judge_agreement"]
    out += [
        "## judge 일치율 (사람 라벨 기준)",
        "",
        "| 대상 | 일치율 | 표본 |",
        "|---|---|---|",
        f"| 결론 judge | {_pct(a['conclusion'])} | {a['n_conclusion']} |",
        f"| 주장 judge | {_pct(a['claims'])} | {a['n_claims']} |",
        "",
    ]
    if a["low"]:
        out += [
            f"> 일치율이 {AGREEMENT_MIN:.0%} 미만이다. LLM 지표는 라벨 표본에서만 유효하다.",
            "",
        ]
    if baseline is not None:
        rows, cur, base = _regression_rows(summary, baseline)
        out += [
            "## 회귀 (baseline 대비 통과 수)",
            "",
            "| 시나리오 | 통과 변화 | 분류 변화 | 판정 |",
            "|---|---|---|---|",
            *rows,
            "",
            f"L1 통과 합계: {base} → {cur} ({cur - base:+d})",
            "",
        ]
    if summary["rules"]:
        out += ["## 규칙 조건 (rules-only)", "", "| 시나리오 | 통과율 | 무효 |", "|---|---|---|"]
        out += [f"| {sid} | {_ci(p, n)} | {inv} |" for sid, (p, n, inv) in summary["rules"].items()]
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="묶음 집계 리포트")
    p.add_argument("batch")
    p.add_argument("--baseline")
    p.add_argument("--out", default=str(ROOT / "eval" / "report.md"))
    args = p.parse_args(argv)
    labels = load_labels(LABELS)

    def build(batch: str) -> dict:
        results = load_batch(HISTORY, batch)
        if not results:
            raise SystemExit(f"batch {batch!r} 결과가 eval/history에 없다")
        return summarize(results, labels, SCENARIOS)

    summary = build(args.batch)
    text = render(summary, build(args.baseline) if args.baseline else None)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
