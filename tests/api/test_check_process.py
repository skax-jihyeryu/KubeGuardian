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


def pod(name, ready=True, restarts=0, owner="ReplicaSet", with_status=True):
    obj = {
        "metadata": {"name": name, "ownerReferences": [{"kind": owner, "name": "x"}]},
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
    ]
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
        "status": {"readyReplicas": ready} if ready else {},
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


def item(verdict, subject, message="m", service=None):
    return {"verdict": verdict, "subject": subject, "message": message, "service": service}


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
    assert deploy.has_findings is True
    assert "W02" not in " ".join(deploy.passed)


def test_stage_without_passed_rules_is_omitted_and_unrun_rules_are_not_listed():
    summary = rule_summary(result([rule("W02", items=[item("detected", "x")])]))
    assert summary.stages == ()


def test_advice_groups_targets_by_rule_with_action_text_and_limit():
    items = [item("detected", f"dev/Service/s{i}", service=f"s{i}") for i in range(8)]
    summary = rule_summary(result([rule("H02", items=items), rule("H01", passed=1)]))
    assert [a.rule_id for a in summary.advice] == ["H02"]
    advice = summary.advice[0]
    assert "CPU·메모리 요청·제한이 없습니다" in advice.action
    assert advice.targets == ("s0", "s1", "s2", "s3", "s4", "s5", "외 2개")
    assert summary.findings == ()


def test_h01_action_text():
    summary = rule_summary(
        result([rule("H01", items=[item("detected", "d/Service/a", service="a")])])
    )
    assert "준비 상태 검사가 포트만 확인합니다" in summary.advice[0].action


def test_developer_lines_and_excluded_rule_names():
    summary = rule_summary(result([rule("S01", passed=1)], excluded=["W02"]))
    assert summary.developer_lines and summary.developer_lines[0].startswith("S01 입력: ")
    assert summary.excluded == (CATALOG["W02"].name,)
