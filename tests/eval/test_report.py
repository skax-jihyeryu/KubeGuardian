"""묶음 집계 리포트: 무효 제외, 라벨 재분류, judge 일치율, 회귀, Wilson, 커버리지 KPI."""

import json

import yaml

from report import (
    apply_label,
    batch_drift,
    load_batch,
    load_labels,
    render,
    summarize,
    wilson,
)

KEY_OK = {"key_found": ["k"], "key_missing": [], "key_all_found": True, "tool_limited": False}


def make_run(failure="pass", *, judge=True, cause=True, seconds=10.0, tokens=(100, 50), **extra):
    run = {
        "status": "done",
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1], "seconds": seconds},
        "passed": failure == "pass",
        "eval": {"cause_match": cause, "forbidden_cause": False, **KEY_OK},
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


def test_scenario_judge_match_rate_reads_top_level(tmp_path):
    results, scen = build(tmp_path, {"A1": [make_run(judge=True), make_run(judge=False)]})
    assert summarize(results, {}, scen)["scenarios"]["A1"]["judge_match"] == 0.5
    results, scen = build(tmp_path / "z", {"A1": [make_run(), make_run()]})
    assert summarize(results, {}, scen)["scenarios"]["A1"]["judge_match"] == 1.0


def test_invalid_runs_excluded_from_denominator(tmp_path):
    runs = [make_run(), make_run(), make_run("F0", invalid="근거 DB 없음"), make_run("error")]
    results, scen = build(tmp_path, {"A1": runs})
    s = summarize(results, {}, scen)["scenarios"]["A1"]
    assert (s["passed"], s["valid"], s["invalid"], s["errors"]) == (2, 3, 1, 1)


def test_label_overrides_judge_and_reclassifies():
    run = make_run("F4", judge=False)
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
        ("t0-A1", 0): {"index": 0, "top1_match": False, "claims": []},
        ("t0-A1", 1): {"index": 1, "top1_match": True, "claims": []},
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


def test_gate_c_skipped_counts_as_pass_and_gate_fail_separate(tmp_path):
    skipped = {"status": "skipped", "passed": True, "legacy_passed": True}
    ran = {"status": "done", "passed": False, "fail_reason": "기대 게이트 C인데 조사가 실행됐다"}
    results, scen = build(tmp_path, {"C1": [skipped, skipped, ran]})
    s = summarize(results, {}, scen)["scenarios"]["C1"]
    assert s["passed"] == 2 and s["valid"] == 3
    assert s["dist"] == {"gate_fail": 1, "skip": 2}


def test_claim_agreement_only_counts_labeled_sources(tmp_path):
    run = make_run(
        claims=[
            {"source": "conclusion", "verdict": "supported", "reason": ""},
            {"source": "issue", "verdict": "supported", "reason": ""},
        ]
    )
    results, scen = build(tmp_path, {"A1": [run]})
    labels = {
        ("t0-A1", 0): {
            "index": 0,
            "top1_match": True,
            "claims": [{"source": "conclusion", "verdict": "unsupported"}],
        }
    }
    a = summarize(results, labels, scen)["judge_agreement"]
    assert a["n_claims"] == 1 and a["claims"] == 0.0


def test_label_index_is_zero_based(tmp_path):
    results, scen = build(tmp_path, {"A1": [make_run("F4", judge=False), make_run()]})
    labels = {("t0-A1", 0): {"index": 0, "top1_match": True, "claims": []}}
    s = summarize(results, labels, scen)["scenarios"]["A1"]
    assert s["passed"] == 2


def test_result_level_invalid(tmp_path):
    history, scen = tmp_path / "history", tmp_path / "scenarios"
    write_result(history, "a", "A1", [make_run(), make_run()], invalid="원복 확인 실패")
    write_result(history, "b", "A1", [], invalid="주입 실패")
    write_result(
        history, "r", "A1", [], condition="rules-only", invalid="x", score={"passed": True}
    )
    write_scenario(scen, "A1")
    summary = summarize(load_batch(history, "b1"), {}, scen)
    s = summary["scenarios"]["A1"]
    assert (s["valid"], s["invalid"], s["passed"]) == (0, 3, 0)
    assert summary["rules"]["A1"] == (0, 0, 1)


def test_regression_shows_distribution_change(tmp_path):
    base, scen = build(tmp_path / "x", {"A1": [make_run("F3", cause=False)] * 2 + [make_run()]})
    cur, _ = build(tmp_path / "y", {"A1": [make_run()] * 3})
    text = render(summarize(cur, {}, scen), summarize(base, {}, scen))
    assert "F3 2→0" in text and "pass 1→3" in text


def test_invalid_round_alone_is_counted_per_round_when_result_has_no_invalid(tmp_path):
    # run.py가 만드는 모양: 회차 하나가 F0(invalid)이고 result["invalid"]는 없다
    f0 = make_run("F0", invalid="근거 id가 DB에 없음")
    s = build(tmp_path, {"A1": [f0, make_run(), make_run()]})
    scenario = summarize(*s[:1], {}, s[1])["scenarios"]["A1"]
    assert (scenario["total"], scenario["valid"], scenario["invalid"]) == (3, 2, 1)
    assert scenario["passed"] == 2


def _rules_failed_batch(tmp_path):
    history, scen = tmp_path / "history", tmp_path / "scenarios"
    write_result(history, "a", "A1", [make_run(), make_run()], score={"passed": False})
    write_result(history, "b", "A1", [make_run()], score={"passed": True})
    write_scenario(scen, "A1")
    return summarize(load_batch(history, "b1"), {}, scen)


def test_ai_pass_requires_rules_score_pass(tmp_path):
    summary = _rules_failed_batch(tmp_path)
    s = summary["scenarios"]["A1"]
    assert (s["passed"], s["valid"], s["rules_fail"]) == (1, 3, 2)
    assert s["dist"] == {"pass": 3}  # 분류 분포는 에이전트 분류 그대로
    assert summary["by_level"]["L1"] == [1, 3]
    text = render(summary, None)
    assert "규칙 실패" in text


def test_kpi_excludes_skip_and_not_run_but_keeps_gate_fail(tmp_path):
    skip = {"status": "skipped", "passed": True, "usage": {"seconds": 0, "input_tokens": 0}}
    not_run = {"status": "skipped", "passed": False}
    gate_fail = {
        "status": "done",
        "passed": False,
        "fail_reason": "기대 게이트 C인데 조사가 실행됐다",
        "usage": {"seconds": 30.0, "input_tokens": 300, "output_tokens": 0},
        "eval": {"cause_match": False},
        "judge_match": False,
    }
    ran = make_run(seconds=10.0, tokens=(100, 0))
    results, scen = build(tmp_path, {"A1": [ran, skip, not_run, gate_fail]})
    summary = summarize(results, {}, scen)
    k = summary["kpi"]
    assert k["accuracy"] == 0.5  # ran 통과 1 / (ran + gate_fail) 2
    assert k["seconds"] == 20.0 and k["tokens"] == 200
    s = summary["scenarios"]["A1"]
    assert s["tokens"] == 200 and s["seconds"] == 20.0


def test_overconfirm_and_underconfirm_counts(tmp_path):
    runs = [make_run(judgement="confirmed") for _ in range(3)]
    runs += [make_run("F2", judgement="confirmed"), make_run("F3", judgement="confirmed")]
    runs += [make_run(judgement="candidate"), make_run("F2", judgement="candidate")]
    results, scen = build(tmp_path, {"A1": runs})
    summary = summarize(results, {}, scen)
    k = summary["kpi"]
    assert (k["overconfirmed"], k["confirmed"], k["overconfirm_rate"]) == (2, 5, 0.4)
    assert k["underconfirmed"] == 1  # candidate pass만. candidate F2는 어느 쪽에도 안 센다
    assert "40% (2/5)" in render(summary, None)


def test_underconfirm_ignores_runs_without_judgement(tmp_path):
    results, scen = build(tmp_path, {"A1": [make_run(), make_run(judgement="guess")]})
    k = summarize(results, {}, scen)["kpi"]
    assert k["underconfirmed"] == 1


def test_overconfirm_excludes_invalid_and_skipped_runs(tmp_path):
    skip = {"status": "skipped", "passed": True, "judgement": "confirmed"}
    runs = [
        make_run(judgement="confirmed"),
        make_run("F0", invalid="x", judgement="confirmed"),
        skip,
    ]
    results, scen = build(tmp_path, {"A1": runs})
    k = summarize(results, {}, scen)["kpi"]
    assert (k["overconfirmed"], k["confirmed"]) == (0, 1)


def test_overconfirm_renders_dash_without_confirmed(tmp_path):
    results, scen = build(tmp_path, {"A1": [make_run(judgement="candidate")]})
    summary = summarize(results, {}, scen)
    assert summary["kpi"]["overconfirm_rate"] is None
    assert "| 오확정률 (confirmed 중 pass 아님) | - |" in render(summary, None)


def test_label_reclassified_confirmed_run_counts_as_overconfirmed(tmp_path):
    results, scen = build(tmp_path, {"A1": [make_run(judgement="confirmed")]})
    label = {
        "index": 0,
        "top1_match": False,
        "claims": [{"source": "conclusion", "verdict": "supported"}],
    }
    k = summarize(results, {("t0-A1", 0): label}, scen)["kpi"]
    assert (k["overconfirmed"], k["confirmed"]) == (1, 1)


def _drift_batch(tmp_path, specs):
    """specs: (시나리오, in_cluster 또는 None, 결과 추가 필드) 목록. 이름 순번이 시각순이다."""
    history = tmp_path / "history"
    for i, (sid, in_cluster, extra) in enumerate(specs):
        if in_cluster is not None:
            extra = {**extra, "in_cluster": in_cluster}
        write_result(history, f"{i:02d}-{sid}", sid, extra.pop("runs", [make_run()]), **extra)
    return load_batch(history, "b1")


def _cluster(config="1", image="sha256:" + "a" * 64):
    return {"pod": "p", "image_id": image, "config_resource_version": config}


def test_batch_drift_reproduces_w4_base(tmp_path):
    results = _drift_batch(
        tmp_path,
        [
            ("A1", _cluster("1614530"), {}),
            ("A3", _cluster("1614530"), {}),
            ("A4", _cluster("1614530"), {}),
            ("A5", _cluster("1617267"), {"invalid": "증상 없음", "runs": []}),
            ("B1", _cluster("1617267"), {}),
        ],
    )
    assert batch_drift(results) == ["config_resource_version: 1614530 → 1617267 (A5부터)"]
    scen = tmp_path / "scenarios"
    text = render(summarize(results, {}, scen), None)
    warning = (
        "> **경고: 평가 도중 클러스터가 바뀌었습니다.** "
        "이 묶음의 결과는 같은 조건이 아니므로 비교에 쓰지 마세요."
    )
    assert text.startswith(f"# 평가 리포트\n\n{warning}\n> - config_resource_version: 1614530")


def test_batch_drift_shows_last_12_chars_of_image_id(tmp_path):
    results = _drift_batch(
        tmp_path,
        [
            ("A1", _cluster(image="sha256:" + "a" * 64), {}),
            ("A3", _cluster(image="sha256:" + "a" * 52 + "b" * 12), {}),
        ],
    )
    assert batch_drift(results) == [f"image_id: {'a' * 12} → {'b' * 12} (A3부터)"]


def test_batch_drift_empty_when_same_and_no_warning(tmp_path):
    results = _drift_batch(tmp_path, [("A1", _cluster(), {}), ("A3", _cluster(), {})])
    assert batch_drift(results) == []
    assert "경고" not in render(summarize(results, {}, tmp_path / "scenarios"), None)


def test_batch_drift_skips_results_without_in_cluster(tmp_path):
    results = _drift_batch(
        tmp_path,
        [("A1", _cluster("1"), {}), ("A3", None, {}), ("A4", _cluster("1"), {})],
    )
    assert batch_drift(results) == []
