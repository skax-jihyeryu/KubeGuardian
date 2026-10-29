"""묶음 집계 리포트: 무효 제외, 라벨 재분류, judge 일치율, 회귀, Wilson, 커버리지 KPI."""

import json

import yaml

from report import apply_label, load_batch, load_labels, render, summarize, wilson

KEY_OK = {"key_found": ["k"], "key_missing": [], "key_all_found": True, "tool_limited": False}


def make_run(failure="pass", *, judge=True, cause=True, seconds=10.0, tokens=(100, 50), **extra):
    run = {
        "status": "done",
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1], "seconds": seconds},
        "passed": failure == "pass",
        "eval": {"cause_match": cause, "forbidden_cause": False, "judge_match": judge, **KEY_OK},
        "claims": [{"source": "conclusion", "verdict": "supported", "reason": ""}],
        "support_rate": 1.0,
        "conclusion_support": "supported",
        "judge_match": judge,
        "failure": failure,
    }
    run.update(extra)
    return run


def write_result(history, name, scenario, runs, batch="b1", condition="ai", **extra):
    d = history / name
    d.mkdir(parents=True)
    result = {"batch": batch, "condition": condition, "scenario": scenario, "ai_runs": runs}
    result.update(extra)
    (d / "result.json").write_text(json.dumps(result), encoding="utf-8")


def write_scenario(root, sid, level="L1", visible=True, gate="A"):
    d = root / sid
    d.mkdir(parents=True)
    (d / "expected.yaml").write_text(
        yaml.safe_dump({"id": sid, "level": level, "k8s_visible": visible, "gate_path": gate}),
        encoding="utf-8",
    )


def build(tmp_path, runs_by_scenario, **kw):
    history, scen = tmp_path / "history", tmp_path / "scenarios"
    for i, (sid, runs) in enumerate(runs_by_scenario.items()):
        write_result(history, f"t{i}-{sid}", sid, runs, batch=kw.get("batch", "b1"))
        write_scenario(scen, sid, **kw.get("info", {}).get(sid, {}))
    return load_batch(history, kw.get("batch", "b1")), scen


def test_load_batch_filters_by_batch(tmp_path):
    history = tmp_path / "history"
    write_result(history, "a", "A1", [], batch="b1")
    write_result(history, "b", "A1", [], batch="b2")
    assert [n for n, _ in load_batch(history, "b1")] == ["a"]


def test_invalid_runs_excluded_from_denominator(tmp_path):
    runs = [make_run(), make_run(), make_run("F0", invalid="근거 DB 없음"), make_run("error")]
    results, scen = build(tmp_path, {"A1": runs})
    s = summarize(results, {}, scen)["scenarios"]["A1"]
    assert (s["passed"], s["valid"], s["invalid"], s["errors"]) == (2, 3, 1, 1)


def test_label_overrides_judge_and_reclassifies():
    run = make_run("F4", judge=False)
    run["eval"]["judge_match"] = False
    label = {
        "index": 1,
        "top1_match": True,
        "claims": [{"source": "conclusion", "verdict": "supported"}],
    }
    out = apply_label(run, label)
    assert out["failure"] == "pass" and out["passed"] is True
    assert out["orig_judge_match"] is False
    assert apply_label(run, None)["failure"] == "F4"


def test_label_claim_verdict_changes_support(tmp_path):
    run = make_run("pass")
    label = {
        "index": 1,
        "top1_match": True,
        "claims": [{"source": "conclusion", "verdict": "unsupported"}],
    }
    out = apply_label(run, label)
    assert out["failure"] == "F5" and out["support_rate"] == 0.0


def test_load_labels(tmp_path):
    d = tmp_path / "labels"
    d.mkdir()
    (d / "t0-A1.yaml").write_text(
        yaml.safe_dump({"runs": [{"index": 2, "top1_match": True, "claims": [], "note": "x"}]}),
        encoding="utf-8",
    )
    assert load_labels(d)[("t0-A1", 2)]["top1_match"] is True
    assert load_labels(tmp_path / "none") == {}


def test_judge_agreement_below_90_flags_table(tmp_path):
    runs = [make_run(judge=True) for _ in range(2)]
    results, scen = build(tmp_path, {"A1": runs})
    labels = {
        ("t0-A1", 1): {"index": 1, "top1_match": False, "claims": []},
        ("t0-A1", 2): {"index": 2, "top1_match": True, "claims": []},
    }
    summary = summarize(results, labels, scen)
    assert summary["judge_agreement"]["conclusion"] == 0.5
    assert summary["judge_agreement"]["low"] is True
    assert "LLM 지표는 라벨 표본에서만 유효" in render(summary, None)
    ok = summarize(results, {k: {**v, "top1_match": True} for k, v in labels.items()}, scen)
    assert ok["judge_agreement"]["low"] is False
    assert "LLM 지표는 라벨 표본에서만 유효" not in render(ok, None)


def test_regression_flag_when_passes_drop_by_two(tmp_path):
    full = [make_run() for _ in range(3)]
    one = [make_run(), make_run("F3", cause=False), make_run("F3", cause=False)]
    two = [make_run(), make_run(), make_run("F3", cause=False)]
    base, scen = build(tmp_path / "x", {"A1": full, "A3": full})
    cur, _ = build(tmp_path / "y", {"A1": one, "A3": two}, batch="b1")
    text = render(summarize(cur, {}, scen), summarize(base, {}, scen))
    lines = {
        line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| A")
    }
    assert "3 → 1" in lines["A1"] and "회귀" in lines["A1"]
    assert "3 → 2" in lines["A3"] and "회귀" not in lines["A3"]
    assert "L1" in text


def test_wilson_known_value():
    lo, hi = wilson(3, 3)
    assert round(lo, 4) == 0.4385
    assert round(hi, 4) == 1.0
    assert wilson(0, 0) == (0.0, 0.0)


def test_coverage_kpi_uses_only_k8s_invisible_scenarios(tmp_path):
    good, bad = [make_run()], [make_run("F3", cause=False)]
    results, scen = build(
        tmp_path,
        {"A1": bad, "H1": good},
        info={"A1": {"visible": True}, "H1": {"visible": False, "level": "L3"}},
    )
    k = summarize(results, {}, scen)["kpi"]
    assert k["coverage"] == 1.0
    assert k["accuracy"] == 0.5


def test_rules_only_table_and_limited(tmp_path):
    history, scen = tmp_path / "history", tmp_path / "scenarios"
    write_result(history, "r", "A1", [], condition="rules-only", score={"passed": True})
    write_result(history, "a", "A1", [make_run("F2", limited=True), make_run()])
    write_scenario(scen, "A1")
    summary = summarize(load_batch(history, "b1"), {}, scen)
    assert summary["scenarios"]["A1"]["limited_failures"] == 1
    text = render(summary, None)
    assert "규칙 조건" in text
