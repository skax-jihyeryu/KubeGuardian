"""점검 과정 표시 (#175): 정보 수집 요약, 점검 규칙 결과."""

from datetime import UTC, datetime

from api.viewmodels.checks import collect_summary
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
