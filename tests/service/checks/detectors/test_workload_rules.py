"""W01·W02 (05-rules §4, §7)."""

from datetime import timedelta

from tests.service.checks.detectors.cases import NOW, context, one, run, snapshot

AGENT = "dev/Deployment/agent"


# ── W01 가용 replicas 부족 ──


def test_w01_passes_when_available_matches():
    s = one(run("W01", context(snapshot())), AGENT)
    assert s.verdict == "passed"
    assert (s.values["replicas"], s.values["availableReplicas"]) == (2, 2)


def test_w01_first_observation_waits_10_minutes_for_updated_replicas(obs):
    # 사용자 확인 필요(임시): 첫 관측에서는 updatedReplicas 변경 시각을 최초 관측 시각으로 본다
    s = one(run("W01", context(snapshot("w01/detected"), observations=obs)), AGENT)
    assert s.verdict == "passed"
    assert s.values["updatedReplicas_stable"] is False
    assert obs.get("W01", AGENT).values == {
        "updatedReplicas": 2,
        "updatedReplicas_changed_at": NOW.isoformat(),
    }


def _seed_w01(obs, changed_ago, updated=2):
    changed = NOW - changed_ago
    obs.seed(
        "W01",
        AGENT,
        changed,
        {"updatedReplicas": updated, "updatedReplicas_changed_at": changed.isoformat()},
    )


def test_w01_holds_15_minutes_after_updated_replicas_stable_for_10(obs):
    _seed_w01(obs, timedelta(minutes=24))
    s = one(run("W01", context(snapshot("w01/detected"), observations=obs)), AGENT)
    assert s.verdict == "observing"

    _seed_w01(obs, timedelta(minutes=25))
    s = one(run("W01", context(snapshot("w01/detected"), observations=obs)), AGENT)
    assert (s.verdict, s.severity) == ("detected", "warning")  # 일부 가용
    assert s.values["updatedReplicas_changed_at"] == (NOW - timedelta(minutes=25)).isoformat()


def test_w01_is_critical_on_core_path_with_none_available(obs):
    _seed_w01(obs, timedelta(hours=1))
    s = one(run("W01", context(snapshot("w01/none_available"), observations=obs)), AGENT)
    assert (s.verdict, s.severity) == ("detected", "critical")


def test_w01_excludes_rollout_in_progress_and_resets_change_time(obs):
    _seed_w01(obs, timedelta(hours=1), updated=2)
    s = one(run("W01", context(snapshot("w01/rolling"), observations=obs)), AGENT)
    assert s.verdict == "passed"
    assert obs.get("W01", AGENT).values["updatedReplicas_changed_at"] == NOW.isoformat()


def test_w01_clears_record_when_recovered(obs):
    _seed_w01(obs, timedelta(hours=1))
    run("W01", context(snapshot(), observations=obs))
    assert obs.get("W01", AGENT) is None


# ── W02 롤아웃 정체 ──


def test_w02_passes_while_progressing():
    s = one(run("W02", context(snapshot())), AGENT)
    assert s.verdict == "passed"
    assert s.values["progressing"]["status"] == "True"


def test_w02_detects_after_15_minutes_and_warns_when_old_replicaset_serves(obs):
    assert one(run("W02", context(snapshot("w02/detected"), observations=obs)), AGENT).verdict == (
        "observing"
    )
    obs.seed("W02", AGENT, NOW - timedelta(minutes=15))
    s = one(run("W02", context(snapshot("w02/detected"), observations=obs)), AGENT)
    assert (s.verdict, s.severity) == ("detected", "warning")
    assert s.values["progressing"]["reason"] == "ProgressDeadlineExceeded"
    assert s.values["progressDeadlineSeconds"] == 600
    assert s.values["new_replicaset"] == {"name": "agent-5f8d7c6b9", "ready": 0}
    assert s.values["old_replicasets"] == [{"name": "agent-7c9f5d6b4", "ready": 2}]


def test_w02_is_critical_when_no_old_pod_serves(obs):
    obs.seed("W02", AGENT, NOW - timedelta(minutes=20))
    s = one(run("W02", context(snapshot("w02/no_old_ready"), observations=obs)), AGENT)
    assert (s.verdict, s.severity) == ("detected", "critical")


def test_w02_is_still_observing_at_14_minutes(obs):
    obs.seed("W02", AGENT, NOW - timedelta(minutes=14))
    s = one(run("W02", context(snapshot("w02/detected"), observations=obs)), AGENT)
    assert s.verdict == "observing"
