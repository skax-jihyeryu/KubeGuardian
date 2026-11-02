"""점검 과정 표시 (#175): 정보 수집 요약, 점검 규칙 결과."""

from datetime import UTC, datetime

from api.viewmodels.checks import collect_summary
from service.checks.detectors.catalog import CATALOG
from service.model import ClusterSnapshot

NOW = datetime(2026, 11, 2, 5, 5, tzinfo=UTC)


def snap(resources=None, failed=None):
    return ClusterSnapshot(
        namespace="dev", collected_at=NOW, resources=resources or {}, failed=failed or {}
    )


def pod(name, ready=True, restarts=0, owner="ReplicaSet", with_status=True, owner_name=None):
    owner_name = owner_name or name.rsplit("-", 1)[0]
    obj = {
        "metadata": {"name": name, "ownerReferences": [{"kind": owner, "name": owner_name}]},
    }
    if with_status:
        obj["status"] = {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [{"name": "c", "restartCount": restarts}],
        }
    return obj


def rows_by_kind(snapshot):
    return {r.kind: r for r in collect_summary(snapshot)}


def test_pod_row_counts_ready_and_restarts_with_hash_stripped_names():
    pods = [pod(f"agent-7d9f8c6b5-{s}") for s in ("aaaaa", "bbbbb", "ccccc")] + [
        pod("web-5c4d7b8f9-zzzzz")
    ]  # 소유 ReplicaSet은 agent-7d9f8c6b5 / web-5c4d7b8f9
    row = rows_by_kind(snap({"Pod": pods}))["Pod"]
    assert row.count == 4
    assert row.status == "Ready 4 · 재시작 0"
    assert row.names == ("agent ×3", "web")
    assert row.primary is True


def test_pod_row_shows_not_ready_and_total_restarts():
    pods = [pod("a-1111-aaaaa", restarts=2)] + [pod(f"b-2222-{i}") for i in range(8)]
    pods.append(pod("c-3333-ccccc", ready=False))
    row = rows_by_kind(snap({"Pod": pods}))["Pod"]
    assert row.status == "Ready 9 · 준비 안 됨 1 · 재시작 2"


def test_pod_without_status_is_counted_as_unknown_not_error():
    row = rows_by_kind(snap({"Pod": [pod("a-1-b", with_status=False)]}))["Pod"]
    assert row.status == "Ready 0 · 확인 불가 1 · 재시작 0"


def deploy(name, replicas, ready):
    return {
        "metadata": {"name": name},
        "spec": {"replicas": replicas},
        "status": {"availableReplicas": ready} if ready else {},
    }


def test_deployment_row_matches_and_flags_shortage():
    ok = rows_by_kind(snap({"Deployment": [deploy("a", 2, 2), deploy("b", 1, 1)]}))["Deployment"]
    assert ok.status == "기대 수와 일치 2"
    assert ok.names == ("a", "b")
    bad = rows_by_kind(snap({"Deployment": [deploy("a", 2, 2), deploy("agent", 3, 1)]}))
    assert bad["Deployment"].status == "기대 수와 일치 1 · 부족 1 (agent 1/3)"


def test_service_row_only_mentions_selectorless_services():
    plain = {"metadata": {"name": "a"}, "spec": {"selector": {"app": "a"}}}
    none = {"metadata": {"name": "ext"}, "spec": {}}
    assert rows_by_kind(snap({"Service": [plain]}))["Service"].status == ""
    row = rows_by_kind(snap({"Service": [plain, none]}))["Service"]
    assert row.status == "selector 없음 1"
    assert row.names == ("a", "ext")


def test_node_row_counts_ready():
    node = {
        "metadata": {"name": "n1"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    row = rows_by_kind(snap({"Node": [node]}))["Node"]
    assert row.status == "Ready 1"
    assert row.names == ("n1",)


def test_other_kinds_are_collapsed_without_names():
    row = rows_by_kind(snap({"ConfigMap": [{"metadata": {"name": "c"}}]}))["ConfigMap"]
    assert (row.count, row.status, row.names, row.primary) == (1, "", (), False)


def test_names_are_limited_to_eight_with_remainder():
    svcs = [{"metadata": {"name": f"s{i}"}, "spec": {"selector": {}}} for i in range(10)]
    row = rows_by_kind(snap({"Service": svcs}))["Service"]
    assert len(row.names) == 9
    assert row.names[-1] == "외 2개"


def test_failed_kind_shows_reason():
    row = rows_by_kind(snap(failed={"ConfigMap": "Forbidden"}))["ConfigMap"]
    assert row.failed == "Forbidden"
    assert row.count == 0


def test_no_snapshot_gives_no_rows():
    assert collect_summary(None) == []


# ── 점검 규칙 결과 ──────────────────────────────────────────────

from api.viewmodels.checks import rule_summary  # noqa: E402
from core.db.check_results import CheckResult  # noqa: E402


def result(verdicts, excluded=()):
    return CheckResult(
        run_id=1,
        status="done",
        started_at=NOW,
        finished_at=NOW,
        gate_path="C",
        gate_reason="",
        first_severity=None,
        verdicts=verdicts,
        symptoms=[],
        phases=[],
        excluded_rules=list(excluded),
        error=None,
    )


def rule(rid, passed=0, items=()):
    return {"rule_id": rid, "passed": passed, "items": list(items)}


def item(verdict, subject, message="m", service=None, values=None):
    return {
        "verdict": verdict,
        "subject": subject,
        "message": message,
        "service": service,
        "values": values or {},
    }


def test_findings_list_detected_before_unknown_and_observing_with_rule_names():
    summary = rule_summary(
        result(
            [
                rule("S01", items=[item("unknown", "dev/Service/a", "못 읽음")]),
                rule("W02", items=[item("detected", "dev/Deployment/agent", "롤아웃 정체")]),
                rule("P03", items=[item("observing", "dev/Pod/x")]),
            ]
        )
    )
    assert [(f.status, f.rule_id) for f in summary.findings] == [
        ("적발", "W02"),
        ("확인하지 못함", "S01"),
        ("관찰 중", "P03"),
    ]
    first = summary.findings[0]
    assert first.rule_name and first.subject == "dev/Deployment/agent"
    assert first.message == "롤아웃 정체"


def test_rules_without_items_are_grouped_as_passed_by_stage():
    summary = rule_summary(
        result([rule("W01", passed=2), rule("W02", items=[item("detected", "dev/Deployment/a")])])
    )
    deploy = next(s for s in summary.stages if s.name == "3 배포")
    assert [n for n in deploy.passed] == [CATALOG["W01"].name]
    assert "W02" not in " ".join(deploy.passed)


def test_stage_without_passed_rules_is_omitted_and_unrun_rules_are_not_listed():
    summary = rule_summary(result([rule("W02", items=[item("detected", "x")])]))
    assert summary.stages == ()


def test_advice_groups_targets_by_rule_with_action_text_and_limit():
    items = [
        item("detected", f"dev/Service/s{i}", service=f"s{i}", values={"missing": ["limits.cpu"]})
        for i in range(8)
    ]
    summary = rule_summary(result([rule("H02", items=items), rule("H01", passed=1)]))
    assert [a.rule_id for a in summary.advice] == ["H02"]
    advice = summary.advice[0]
    assert advice.action == "CPU·메모리 요청·제한 일부가 없습니다: limits.cpu"
    assert advice.targets == ("s0", "s1", "s2", "s3", "s4", "s5", "외 2개")
    assert summary.findings == ()


def test_h01_action_text_follows_probe_kind():
    none = item("detected", "d/Service/a", service="a", values={"probe": None})
    tcp = item("detected", "d/Service/b", service="b", values={"probe": "tcpSocket"})
    summary = rule_summary(result([rule("H01", items=[none, tcp])]))
    assert {a.action: a.targets for a in summary.advice} == {
        "준비 상태 검사가 없습니다": ("a",),
        "준비 상태 검사가 포트만 확인합니다": ("b",),
    }


def test_advice_rule_unknown_goes_to_findings_not_advice():
    summary = rule_summary(result([rule("H01", items=[item("unknown", "d/Service/a", "못 읽음")])]))
    assert summary.advice == ()
    assert [(f.status, f.rule_id) for f in summary.findings] == [("확인하지 못함", "H01")]


def test_advice_rule_without_action_text_has_empty_action(monkeypatch):
    import api.viewmodels.checks as mod

    monkeypatch.setattr(mod, "_advice_action", lambda rid, values: "")
    summary = rule_summary(
        result([rule("H02", items=[item("detected", "d/Service/a", service="a")])])
    )
    assert summary.advice[0].action == ""


def test_advice_targets_are_deduplicated_before_limit():
    items = [item("detected", f"p{i}", service="web", values={"probe": None}) for i in range(10)]
    summary = rule_summary(result([rule("H01", items=items)]))
    assert summary.advice[0].targets == ("web",)


def test_passed_advice_rule_is_reported_as_not_applicable():
    summary = rule_summary(result([rule("H01", passed=3)]))
    stage = next(s for s in summary.stages if s.name == "개선 권고")
    assert stage.passed == (CATALOG["H01"].name,)


def test_rule_with_no_targets_is_separated_from_passed():
    summary = rule_summary(
        result([rule("W01", passed=0), rule("W02", passed=2), {"rule_id": "S01", "items": []}])
    )
    deploy = next(s for s in summary.stages if s.name == "3 배포")
    assert deploy.passed == (CATALOG["W02"].name,)
    assert deploy.no_target == (CATALOG["W01"].name,)
    connect = next(s for s in summary.stages if s.name == "6 연결")
    assert connect.passed == (CATALOG["S01"].name,)  # passed 필드가 없는 옛 결과는 통과


def test_stages_cover_every_signal_rule_in_the_catalog():
    from api.viewmodels.setup import STAGES

    staged = {rid for _, ids in STAGES for rid in ids}
    missing = [rid for rid, spec in CATALOG.items() if spec.grade == "signal" and rid not in staged]
    assert missing == []


# ── 수집 요약 보완 ───────────────────────────────────────────────


def test_restart_count_includes_init_containers():
    p = pod("a-1-aaaaa", restarts=1)
    p["status"]["initContainerStatuses"] = [{"name": "i", "restartCount": 2}]
    assert rows_by_kind(snap({"Pod": [p]}))["Pod"].status == "Ready 1 · 재시작 3"


def test_succeeded_and_terminating_pods_are_not_counted_as_not_ready():
    done = pod("job-1-aaaaa", ready=False)
    done["status"]["phase"] = "Succeeded"
    gone = pod("web-2-bbbbb", ready=False)
    gone["metadata"]["deletionTimestamp"] = "2026-11-02T05:00:00Z"
    row = rows_by_kind(snap({"Pod": [pod("ok-3-ccccc"), done, gone]}))["Pod"]
    assert row.status == "Ready 1 · 완료 1 · 종료 중 1 · 재시작 0"


def test_unscheduled_pod_without_ready_condition_is_not_ready():
    p = pod("a-1-aaaaa", with_status=False)
    p["status"] = {"conditions": [{"type": "PodScheduled", "status": "False"}]}
    assert rows_by_kind(snap({"Pod": [p]}))["Pod"].status == "Ready 0 · 준비 안 됨 1 · 재시작 0"


def test_pod_names_group_by_replicaset_owner_name():
    a = pod("agent-7d9f8c6b5-aaaaa", owner_name="agent-7d9f8c6b5")
    b = pod("agent-7d9f8c6b5-bbbbb", owner_name="agent-7d9f8c6b5")
    lone = {"metadata": {"name": "solo-pod-1"}}
    row = rows_by_kind(snap({"Pod": [a, b, lone]}))["Pod"]
    assert row.names == ("agent ×2", "solo-pod-1")


def test_node_row_splits_ready_not_ready_unknown():
    def node(name, status=None):
        conds = [{"type": "Ready", "status": status}] if status else []
        return {"metadata": {"name": name}, "status": {"conditions": conds}}

    row = rows_by_kind(snap({"Node": [node("a", "True"), node("b", "False"), node("c")]}))["Node"]
    assert row.status == "Ready 1 · 준비 안 됨 1 · 확인 불가 1"


def test_developer_lines_and_excluded_rule_names():
    summary = rule_summary(result([rule("S01", passed=1)], excluded=["W02"]))
    assert summary.developer_lines and summary.developer_lines[0].startswith("S01 입력: ")
    assert summary.excluded == (CATALOG["W02"].name,)
