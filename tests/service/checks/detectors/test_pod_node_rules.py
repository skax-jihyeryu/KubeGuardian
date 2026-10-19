"""N01·P03·P06·P07 (05-rules §4, §7)."""

from datetime import timedelta

from tests.service.checks.detectors.cases import NOW, context, one, run, snapshot

X2K = "dev/Pod/agent-7c9f5d6b4-x2k9p/agent"


# ── N01 노드 NotReady ──


def test_n01_passes_on_ready_node():
    s = one(run("N01", context(snapshot())), "Node/minikube")
    assert s.verdict == "passed"
    assert s.values["ready"] == "True"


def test_n01_observes_then_detects_after_15_minutes(obs):
    first = one(run("N01", context(snapshot("n01/detected"), observations=obs)), "Node/minikube")
    assert first.verdict == "observing"

    obs.seed("N01", "Node/minikube", NOW - timedelta(minutes=15))
    s = one(run("N01", context(snapshot("n01/detected"), observations=obs)), "Node/minikube")
    assert (s.verdict, s.severity) == ("detected", "critical")
    assert s.values["reason"] == "NodeStatusUnknown"
    assert s.values["lastTransitionTime"] == "2026-10-20T00:40:00Z"


def test_n01_excludes_cordoned_node(obs):
    s = one(run("N01", context(snapshot("n01/excluded"), observations=obs)), "Node/minikube")
    assert s.verdict == "passed"
    assert s.values["unschedulable"] is True
    assert obs.rows == {}


# ── P03 이미지 받기 실패 ──

NEW_POD = "dev/Pod/agent-5f8d7c6b9-n7v2c/agent"


def test_p03_detects_after_1_hour_and_warns_when_old_pods_serve(obs):
    assert one(
        run("P03", context(snapshot("p03/detected"), observations=obs)), NEW_POD
    ).verdict == ("observing")
    obs.seed("P03", NEW_POD, NOW - timedelta(minutes=59))
    assert one(
        run("P03", context(snapshot("p03/detected"), observations=obs)), NEW_POD
    ).verdict == ("observing")

    obs.seed("P03", NEW_POD, NOW - timedelta(hours=1))
    s = one(run("P03", context(snapshot("p03/detected"), observations=obs)), NEW_POD)
    assert (s.verdict, s.severity, s.service) == ("detected", "warning", "agent")
    assert s.values["image"] == "agent:9.9-missing"
    assert s.values["reason"] == "ImagePullBackOff"
    assert s.values["replicaset"] == "agent-5f8d7c6b9"
    assert s.values["ready_pods_in_deployment"] == 2


def test_p03_is_critical_on_core_path_without_ready_pods(obs):
    for pod in ("agent-7c9f5d6b4-x2k9p", "agent-7c9f5d6b4-q8w3z"):
        obs.seed("P03", f"dev/Pod/{pod}/agent", NOW - timedelta(hours=2))
    signals = run("P03", context(snapshot("p03/all_failing"), observations=obs))
    assert {(s.verdict, s.severity) for s in signals if s.service == "agent"} == {
        ("detected", "critical")
    }


def test_p03_counts_err_image_never_pull(obs):
    # imagePullPolicy: Never인데 노드에 이미지가 없음. 이미지를 받지 못한다는 의미가 같다
    from service.sensors.collector import collect
    from tests.service.checks.detectors.cases import FixtureReader, raw_cluster

    objects, failed = raw_cluster("p03/detected")
    pod = next(p for p in objects["Pod"] if p["metadata"]["name"] == "agent-5f8d7c6b9-n7v2c")
    pod["status"]["containerStatuses"][0]["state"]["waiting"]["reason"] = "ErrImageNeverPull"
    obs.seed("P03", NEW_POD, NOW - timedelta(hours=1))
    snap = collect(FixtureReader(objects, failed), now=NOW)
    s = one(run("P03", context(snap, observations=obs)), NEW_POD)
    assert (s.verdict, s.values["reason"]) == ("detected", "ErrImageNeverPull")


def test_p03_ignores_crash_loop_waiting():
    assert one(run("P03", context(snapshot("p03/excluded"))), X2K).verdict == "passed"


# ── P06 반복 재시작 ──


def test_p06_detects_crash_loop_after_15_minutes(obs):
    obs.seed("P06", X2K, NOW - timedelta(minutes=14))
    assert one(run("P06", context(snapshot("p06/detected"), observations=obs)), X2K).verdict == (
        "observing"
    )
    obs.seed("P06", X2K, NOW - timedelta(minutes=15))
    s = one(run("P06", context(snapshot("p06/detected"), observations=obs)), X2K)
    assert (s.verdict, s.severity) == ("detected", "warning")  # 다른 agent Pod는 Ready
    assert s.values["restartCount"] == 5
    assert s.values["waiting_reason"] == "CrashLoopBackOff"
    assert s.values["last_terminated"]["exitCode"] == 1


def test_p06_counts_restart_increase_since_previous_check(obs):
    prev = snapshot("p06/past_restarts", now=NOW - timedelta(minutes=5))  # restartCount 3
    s = one(
        run("P06", context(snapshot("p06/restarted_running"), previous=prev, observations=obs)), X2K
    )
    assert s.verdict == "observing"
    assert (s.values["restartCount"], s.values["previous_restartCount"]) == (4, 3)


def test_p06_excludes_recovered_past_restarts(obs):
    prev = snapshot("p06/past_restarts", now=NOW - timedelta(minutes=5))
    obs.seed("P06", X2K, NOW - timedelta(minutes=30))
    s = one(
        run("P06", context(snapshot("p06/past_restarts"), previous=prev, observations=obs)), X2K
    )
    assert s.verdict == "passed"
    assert ("P06", X2K) not in obs.rows  # 조건이 풀리면 기록을 지운다


def test_p06_without_previous_snapshot_judges_crash_loop_only():
    # 05-rules P06: 직전 스냅샷이 없으면 restartCount 비교 없이 CrashLoopBackOff만 본다
    assert one(run("P06", context(snapshot("p06/restarted_running"))), X2K).verdict == "passed"
    assert one(run("P06", context(snapshot("p06/detected"))), X2K).verdict == "observing"


# ── P07 OOMKilled ──


def test_p07_detects_recent_oom_kill_immediately():
    s = one(run("P07", context(snapshot("p07/detected"))), X2K)
    assert (s.verdict, s.severity) == ("detected", "warning")
    assert s.values["finishedAt"] == "2026-10-20T00:50:00Z"
    assert s.values["resources"]["limits"]["memory"] == "512Mi"


def test_p07_excludes_old_oom_kill():
    s = one(run("P07", context(snapshot("p07/excluded"))), X2K)
    assert s.verdict == "passed"


def test_p07_passes_without_termination():
    assert one(run("P07", context(snapshot())), X2K).verdict == "passed"


def test_n01_is_still_observing_at_14_minutes(obs):
    obs.seed("N01", "Node/minikube", NOW - timedelta(minutes=14))
    s = one(run("N01", context(snapshot("n01/detected"), observations=obs)), "Node/minikube")
    assert s.verdict == "observing"


def test_p07_ignores_finish_time_without_timezone():
    from service.checks.detectors.p07 import _parse

    assert _parse("2026-10-20T00:50:00") is None  # aware now와 비교할 수 없어 판정에 쓰지 않음
    assert _parse("2026-10-20T00:50:00Z") is not None
