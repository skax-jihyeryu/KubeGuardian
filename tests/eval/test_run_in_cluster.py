"""평가 러너 --in-cluster: kubectl exec 래퍼(remote.py)와 run() 연결. subprocess는 가짜다."""

import json
import subprocess

import pytest

from remote import RemoteError, RemoteRunner
from run import check_repeatedly

SIGNAL = {
    "rule_id": "S01",
    "verdict": "detected",
    "subject": "dev/Service/agent",
    "severity": "critical",
    "values": {},
    "message": "",
}


def _check_payload(run_id=1, detected=True, failed=None, symptoms=None):
    signals = [SIGNAL] if detected else []
    return {
        "run_id": run_id,
        "gate_path": "A" if detected else "C",
        "gate": {
            "path": "A" if detected else "C",
            "reason": "적발: S01" if detected else "",
            "signals": signals,
        },
        "signals": signals,
        "snapshot_failed": failed or {},
        "symptoms": symptoms or [],
    }


class FakeKubectl:
    """kubectl 호출을 흉내 낸다. pods는 호출마다 돌려줄 Pod 이름 목록을 소진한다."""

    def __init__(self, pods=("kg-runner-aaa",), execs=()):
        self.pods = list(pods)
        self.execs = list(execs)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "get" in cmd and "configmap" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="4242", stderr="")
        if "get" in cmd and "pod" in cmd:  # Pod 하나의 imageID 조회 (목록 조회는 "pods")
            return subprocess.CompletedProcess(cmd, 0, stdout="docker://sha256:abc", stderr="")
        if "get" in cmd:
            name = self.pods.pop(0) if len(self.pods) > 1 else self.pods[0]
            return subprocess.CompletedProcess(cmd, 0, stdout=name, stderr="")
        if "exec" in cmd:
            r = self.execs.pop(0)
            if isinstance(r, subprocess.CompletedProcess):
                return r
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(r) + "\n", stderr="warn")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _runner(fake, run_name="20261022T000000Z-A1"):
    r = RemoteRunner(run_name, run=fake)
    r.start()
    return r


def test_check_parses_exec_json_into_outcome():
    fake = FakeKubectl(execs=[_check_payload()])

    outcome = _runner(fake).check()

    assert outcome.run_id == 1
    assert outcome.gate.path == "A"
    assert outcome.signals[0].rule_id == "S01"
    assert outcome.snapshot.failed == {}
    exec_cmd = next(c for c in fake.calls if "exec" in c)
    assert "kg-runner-aaa" in exec_cmd
    assert exec_cmd[exec_cmd.index("--") + 3 :][:3] == ["python", "-m", "runner_step"]
    assert "/data/20261022T000000Z-A1/kg.db" in exec_cmd


def test_check_and_investigate_send_symptom_only_when_given():
    skipped = {"skipped": True}
    fake = FakeKubectl(execs=[_check_payload(), skipped, _check_payload(), skipped])
    runner = _runner(fake)

    runner.check()
    runner.investigate(1, "eval-x")
    runner.check("채팅이 가끔 실패")
    runner.investigate(1, "eval-x", "채팅이 가끔 실패")

    execs = [c for c in fake.calls if "exec" in c]
    assert ["--symptom" in c for c in execs] == [False, False, True, True]
    assert execs[2][execs[2].index("--symptom") + 1] == "채팅이 가끔 실패"
    assert execs[3][execs[3].index("--symptom") + 1] == "채팅이 가끔 실패"


def test_check_repeatedly_stops_at_expected_signal_through_exec():
    fake = FakeKubectl(execs=[_check_payload(1, detected=False), _check_payload(2, detected=True)])
    runner = _runner(fake)
    t = [0.0]

    outcome, checks, first = check_repeatedly(
        runner.check,
        {"must_signals": ["S01"], "gate_path": "A"},
        interval_s=300,
        max_s=2700,
        elapsed=lambda: t[0],
        sleep=lambda s: t.__setitem__(0, t[0] + s),
    )

    assert [c["detected"] for c in checks] == [[], ["S01"]]
    assert outcome.run_id == 2
    assert first == {"S01": 300}


def test_nonzero_exit_is_error_with_masked_cause():
    bad = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="Traceback\nAZURE_OPENAI_API_KEY=sk-abcdef1234567890abcd boom"
    )
    runner = _runner(FakeKubectl(execs=[bad]))

    with pytest.raises(RemoteError) as e:
        runner.check()

    assert "exit 1" in str(e.value) and "boom" in str(e.value)
    assert "abcdef1234567890" not in str(e.value)


def test_non_json_stdout_is_error():
    junk = subprocess.CompletedProcess([], 0, stdout="not json\n", stderr="")

    with pytest.raises(RemoteError, match="JSON"):
        _runner(FakeKubectl(execs=[junk])).check()


def test_pod_replaced_during_run_is_error():
    fake = FakeKubectl(pods=["kg-runner-aaa", "kg-runner-bbb"], execs=[_check_payload()])
    runner = _runner(fake)

    with pytest.raises(RemoteError, match="Pod"):
        runner.check()
    assert not any("exec" in c for c in fake.calls)


def test_secret_like_strings_in_payload_are_masked():
    payload = _check_payload()
    payload["signals"][0]["message"] = "DB_PASSWORD=hunter2hunter2 로 접속"

    outcome = _runner(FakeKubectl(execs=[payload])).check()

    assert "hunter2" not in outcome.signals[0].message


def test_investigate_returns_agent_result_and_trace_id():
    payload = {
        "status": "done",
        "report": {"conclusion": "c", "confidence": "high", "impact_scope": "x"},
        "steps": [],
        "usage": {"llm_calls": 2},
        "error": None,
        "trace_id": "abc123",
    }
    fake = FakeKubectl(execs=[payload])

    result, trace_id = _runner(fake).investigate(check_run_id=3, run_id="eval-A1-x")

    assert result.status == "done" and result.report.conclusion == "c"
    assert trace_id == "abc123"
    exec_cmd = next(c for c in fake.calls if "exec" in c)
    assert exec_cmd[exec_cmd.index("--check-run-id") + 1] == "3"


def test_investigate_sends_ai_on_rules_only_when_set():
    fake = FakeKubectl(execs=[{"skipped": True}, {"skipped": True}])
    runner = _runner(fake)

    runner.investigate(1, "r")
    runner.ai_on_rules = True
    runner.investigate(1, "r")

    first, second = [c for c in fake.calls if "exec" in c]
    assert "--ai-on-rules" not in first and "--ai-on-rules" in second


def test_investigate_skipped_returns_none():
    result, trace_id = _runner(FakeKubectl(execs=[{"skipped": True}])).investigate(1, "r")

    assert result is None and trace_id is None


def test_fetch_copies_run_dir_db_into_history(tmp_path):
    fake = FakeKubectl()
    runner = _runner(fake, run_name="RUN1")

    runner.fetch(tmp_path)

    cp = next(c for c in fake.calls if "cp" in c)
    assert cp[-2:] == ["kubeguardian/kg-runner-aaa:/data/RUN1/kg.db", str(tmp_path / "kg.db")]


def test_kubectl_args_never_carry_secret_names():
    fake = FakeKubectl(execs=[_check_payload()])
    _runner(fake).check()

    assert "AZURE_OPENAI" not in " ".join(" ".join(c) for c in fake.calls)


# ── run(): --in-cluster 연결 ──


class StubRemote:
    """RemoteRunner 대역. 점검은 준비한 결과를 돌려주고, Pod 오류는 지정한 호출에서 낸다."""

    def __init__(self, run_name, fail_at=None, outcomes=None):
        self.run_name = run_name
        self.pod = "kg-runner-aaa"
        self.fail_at = fail_at
        self.calls = 0
        self.fetched = None
        self.outcomes = outcomes
        self.fetch_error: Exception | None = None
        self.db_incomplete = False
        self.events: list[str] = []

    def start(self):
        return self.pod

    def describe(self):
        return {"image_id": "docker://sha256:abc", "config_resource_version": "4242"}

    def check(self, symptom=None):
        self.calls += 1
        if self.fail_at == self.calls:
            raise RemoteError("runner Pod가 바뀌었다 (a → b)")
        return self.outcomes(self.calls)

    def fetch(self, dest):
        self.events.append("fetch")
        if self.fetch_error:
            raise self.fetch_error
        self.fetched = dest


def _run_in_cluster(
    tmp_path,
    monkeypatch,
    scenario,
    fail_at=None,
    stub_hook=None,
    symptom_ok=True,
    condition="rules",
    ai_repeats=1,
    ai_on_rules=False,
    legacy_expected=False,
):
    import run as run_mod

    cfg = tmp_path / "kg.yaml"
    cfg.write_text("target: {namespace: dev}\ncore_paths: {chat: [gateway, agent]}\n")
    monkeypatch.setattr(run_mod, "ROOT", tmp_path)
    monkeypatch.setattr(run_mod, "HISTORY", tmp_path / "history")
    if legacy_expected:
        target = tmp_path / "scenarios" / scenario / "expected.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("id: H0\nlevel: L1\nk8s_visible: true\ngate_path: C\n")
        monkeypatch.setattr(run_mod, "SCENARIOS", tmp_path / "scenarios")
    monkeypatch.setattr(run_mod, "until_ok", lambda sc, *a, **k: (symptom_ok or sc == "H0", "ok"))
    events: list[str] = []

    def fake_sh(scenario_id, script):
        events.append(script)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(run_mod, "sh", fake_sh)
    monkeypatch.setattr(
        run_mod,
        "K8sClient",
        type("NoLocalK8s", (), {"from_config": staticmethod(_boom)}),
    )
    stub = StubRemote(
        "x", fail_at=fail_at, outcomes=lambda n: _stub_outcome(detected=(scenario != "H0"))
    )
    if stub_hook:
        stub_hook(stub)
    stub.events = events  # reset.sh와 fetch의 순서를 한 목록으로 본다
    monkeypatch.setattr(run_mod, "RemoteRunner", lambda name: stub)
    code = run_mod.run(
        scenario,
        str(cfg),
        1,
        300,
        2700,
        condition,
        in_cluster=True,
        ai_repeats=ai_repeats,
        ai_on_rules=ai_on_rules,
    )
    result = json.loads(next((tmp_path / "history").glob("*/result.json")).read_text())
    return code, result, stub


def _boom(*a, **k):
    raise AssertionError("--in-cluster는 Mac에서 클러스터를 읽지 않는다")


def _stub_outcome(detected, symptoms=None):
    payload = _check_payload(1, detected=detected, symptoms=symptoms)
    return _runner(FakeKubectl(execs=[payload])).check()


def test_run_in_cluster_h0_passes_and_fetches_db(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0")

    assert code == 0, result
    assert result["in_cluster"] == {
        "pod": "kg-runner-aaa",
        "image_id": "docker://sha256:abc",
        "config_resource_version": "4242",
    }
    assert result["checks"][0]["n"] == 1
    assert result["alert_timing"]["detection"]["status"] == "unsupported"
    assert stub.fetched is not None and stub.fetched.name.endswith("-H0")


def test_run_in_cluster_pod_error_is_invalid_and_still_fetches(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0", fail_at=1)

    assert code == 2
    assert "RemoteError" in result["invalid"] and "Pod" in result["invalid"]
    assert result["recovered"] is True
    assert stub.fetched is not None


def test_reset_runs_before_fetch_and_fetch_failure_does_not_block_it(tmp_path, monkeypatch):
    def hook(stub):
        stub.fetch_error = OSError("disk full AZURE_OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx")

    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0", stub_hook=hook)

    assert stub.events.index("reset.sh") < stub.events.index("fetch")
    assert result["recovered"] is True
    assert "disk full" in result["db_fetch_failed"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in result["db_fetch_failed"]
    assert code == 0, result


def test_symptom_failure_before_any_check_does_not_fetch(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "A1", symptom_ok=False)

    assert code == 2 and result["invalid"] == "증상이 나지 않음"
    assert "fetch" not in stub.events


def test_db_incomplete_is_recorded_when_exec_timed_out(tmp_path, monkeypatch):
    def hook(stub):
        stub.db_incomplete = True

    _, result, _ = _run_in_cluster(tmp_path, monkeypatch, "H0", stub_hook=hook)

    assert result["db_incomplete"] is True


def test_exec_timeout_marks_db_incomplete_and_wraps_command_in_pod_timeout():
    def run(cmd, **kw):
        if "exec" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout="kg-runner-aaa", stderr="")

    runner = RemoteRunner("RUN1", run=run)
    runner.start()
    with pytest.raises(RemoteError, match="시간 초과"):
        runner.check()

    assert runner.db_incomplete is True


def test_exec_command_runs_under_pod_side_timeout():
    fake = FakeKubectl(execs=[_check_payload()])
    _runner(fake).check()

    cmd = next(c for c in fake.calls if "exec" in c)
    i = cmd.index("--")
    assert cmd[i + 1] == "timeout" and cmd[i + 2].isdigit() and cmd[i + 3] == "python"


def test_repeated_checks_send_the_same_db_to_every_exec():
    fake = FakeKubectl(execs=[_check_payload(1), _check_payload(2)])
    runner = _runner(fake, run_name="RUN1")
    runner.check()
    runner.check()

    dbs = [c[c.index("--db") + 1] for c in fake.calls if "exec" in c]
    assert dbs == ["/data/RUN1/kg.db"] * 2


def test_pod_not_found_at_start_is_invalid_and_nothing_is_injected(tmp_path, monkeypatch):
    def hook(stub):
        def boom():
            raise RemoteError("runner Pod를 하나로 찾지 못함")

        stub.start = boom

    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "A1", stub_hook=hook)

    assert code == 2 and "runner Pod 확인 실패" in result["invalid"]
    assert stub.events == []  # inject·reset 어느 것도 실행하지 않았다


def test_ai_path_records_the_trace_id_from_the_pod(tmp_path, monkeypatch):
    def hook(stub):
        stub.investigate = lambda check_run_id, run_id, symptom: (
            None,
            "0af7651916cd43dd8448eb211c80319c",
        )

    import run as run_mod

    monkeypatch.setattr(run_mod, "build_chat_model", lambda *a, **k: None)
    _, result, _ = _run_in_cluster(tmp_path, monkeypatch, "H0", stub_hook=hook, condition="ai")

    assert result["ai_runs"][0]["trace_id"] == "0af7651916cd43dd8448eb211c80319c"


def test_user_symptom_reaches_the_pod_check_and_investigation(tmp_path, monkeypatch):
    import run as run_mod

    monkeypatch.setattr(run_mod, "build_chat_model", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "score_layers", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "read_target", lambda scenario: "agent-a-b")
    seen = {}

    def hook(stub):
        check = stub.check

        def spy_check(symptom=None):
            seen["check"] = symptom
            return check(symptom)

        def spy_investigate(check_run_id, run_id, symptom):
            seen["investigate"] = symptom
            return None, None

        stub.check, stub.investigate = spy_check, spy_investigate

    _, result, _ = _run_in_cluster(
        tmp_path, monkeypatch, "B1", stub_hook=hook, condition="ai", ai_on_rules=True
    )

    assert seen["check"] == seen["investigate"] == result["user_symptom"]
    assert seen["check"]


def test_one_invalid_round_does_not_invalidate_the_whole_run_result(tmp_path, monkeypatch):
    import run as run_mod

    calls = []

    def score_layers(ai, *a, **k):
        calls.append(ai)
        if len(calls) == 2:
            raise ValueError("근거 id가 DB에 없음")

    monkeypatch.setattr(run_mod, "build_chat_model", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "score_layers", score_layers)

    def hook(stub):
        stub.investigate = lambda check_run_id, run_id, symptom: (None, None)

    code, result, _ = _run_in_cluster(
        tmp_path, monkeypatch, "H0", stub_hook=hook, condition="ai", ai_repeats=3
    )

    assert code == run_mod.EXIT_INVALID  # 종료 코드는 하나라도 무효면 2
    assert "invalid" not in result  # 실행 단위 사유가 아니다
    assert [bool(r.get("invalid")) for r in result["ai_runs"]] == [False, True, False]
    assert result["invalid_runs"] == [1]


def test_describe_reports_image_id_and_config_resource_version():
    runner = _runner(FakeKubectl())

    assert runner.describe() == {
        "image_id": "docker://sha256:abc",
        "config_resource_version": "4242",
    }


UNKNOWN = {"scenario": "chat", "status": "unknown", "summary": "조회 실패: Service 목록"}
FAILED = {"scenario": "chat", "status": "failed", "failed_step": 2, "summary": "로그인 500"}


def test_check_restores_symptoms_with_status_from_exec_json():
    fake = FakeKubectl(execs=[_check_payload(symptoms=[FAILED, UNKNOWN])])

    outcome = _runner(fake).check()

    assert [(x.scenario, x.status) for x in outcome.symptoms] == [
        ("chat", "failed"),
        ("chat", "unknown"),
    ]
    assert outcome.symptoms[0].failed_step == 2


def test_unknown_symptom_from_the_pod_makes_the_in_cluster_run_invalid(tmp_path, monkeypatch):
    def hook(stub):
        stub.outcomes = lambda n: _stub_outcome(detected=False, symptoms=[UNKNOWN])

    code, result, _ = _run_in_cluster(tmp_path, monkeypatch, "H0", stub_hook=hook)

    assert code == 2
    assert result["unknown_symptoms"] == ["chat: 조회 실패: Service 목록"]
    assert result["invalid"].startswith("증상 감지 확인하지 못함")


def test_run_captures_end_metadata_after_fetch(tmp_path, monkeypatch):
    def hook(stub):
        def describe():
            stub.events.append("describe")
            return {"image_id": "end" if stub.fetched else "start", "config_resource_version": "1"}

        stub.describe = describe

    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0", stub_hook=hook)
    assert code == 0
    assert result["in_cluster"]["image_id"] == "start"
    assert result["in_cluster_end"]["image_id"] == "end"
    assert stub.events[-2:] == ["fetch", "describe"]


@pytest.mark.parametrize("fail_at", [None, 1])
def test_end_describe_failure_preserves_result_and_invalidates(tmp_path, monkeypatch, fail_at):
    def hook(stub):
        def describe():
            if "fetch" in stub.events:
                raise RemoteError("runner unavailable")
            return {"image_id": "start", "config_resource_version": "1"}

        stub.describe = describe
        stub.fetch_error = RemoteError("fetch unavailable")

    code, result, _ = _run_in_cluster(tmp_path, monkeypatch, "H0", fail_at=fail_at, stub_hook=hook)
    assert code == 2
    assert ("Pod" if fail_at else "runner unavailable") in result["invalid"]
    assert "fetch unavailable" in result["db_fetch_failed"]
    assert "runner unavailable" in result["in_cluster_end_failed"]
    assert "in_cluster_end" not in result


# ── 규칙 감지는 AI 조사 없이 규칙 진단으로 기록한다 (#185) ──


def _no_investigation(stub):
    def investigate(*a, **k):
        raise AssertionError("규칙 감지는 AI 조사를 부르지 않는다")

    stub.investigate = investigate


def test_rule_detection_records_rule_diagnosis_without_ai(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(
        tmp_path, monkeypatch, "A1", stub_hook=_no_investigation, condition="ai"
    )

    assert code == 0, result
    assert result["rule_diagnosis"] == {
        "label": "규칙 진단(AI 미호출)",
        "ai_called": False,
        "gate_path": "A",
        "rules": ["S01"],
        "failed_features": [],
    }
    assert "ai_runs" not in result and "ai_on_rules" not in result
    assert stub.ai_on_rules is False


def test_ai_on_rules_option_investigates_rule_detection_and_marks_result(tmp_path, monkeypatch):
    import run as run_mod

    calls = []

    def hook(stub):
        stub.investigate = lambda check_run_id, run_id, symptom: (
            calls.append(run_id) or (None, None)
        )

    monkeypatch.setattr(run_mod, "build_chat_model", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "score_layers", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "read_target", lambda scenario: None)
    _, result, stub = _run_in_cluster(
        tmp_path, monkeypatch, "A1", stub_hook=hook, condition="ai", ai_on_rules=True
    )

    assert len(calls) == 1 and stub.ai_on_rules is True
    assert result["ai_on_rules"] is True and len(result["ai_runs"]) == 1
    assert "rule_diagnosis" not in result


def test_remote_transports_dry_run_alert_metadata():
    alert = {"id": 1, "incident_id": 1, "kind": "M1", "sent_at": "2026-01-01T00:00:01+00:00"}
    cause = alert | {"id": 2, "kind": "M1u"}
    payload = {
        "status": "done",
        "report": {"conclusion": "c", "confidence": "high", "impact_scope": "x"},
        "steps": [],
        "usage": {"llm_calls": 2},
        "cause_alert": cause,
    }
    runner = _runner(
        FakeKubectl(
            execs=[_check_payload() | {"detection_alert": alert}, payload, {"skipped": True}]
        )
    )

    assert runner.check().detection_alert == alert
    runner.investigate(1, "eval-x")
    assert runner.investigation_alert_supported and runner.investigation_alert == cause
    runner.investigate(1, "eval-y")
    assert runner.investigation_alert_supported and runner.investigation_alert is None


@pytest.mark.parametrize(
    ("legacy_expected", "gate_severity_supported", "status"),
    [(False, True, "match"), (False, False, "unmeasured"), (True, True, None)],
)
def test_runner_preserves_legacy_expected_and_freezes_new_stage_metadata(
    tmp_path, monkeypatch, legacy_expected, gate_severity_supported, status
):
    def stub_runner(stub):
        stub.investigate = lambda *args: (None, None)
        if gate_severity_supported:
            outcome = _stub_outcome(detected=False)
            outcome.gate = outcome.gate.model_copy(update={"severity": None})
            stub.outcomes = lambda _: outcome

    code, result, _ = _run_in_cluster(
        tmp_path,
        monkeypatch,
        "H0",
        condition="ai",
        legacy_expected=legacy_expected,
        stub_hook=stub_runner,
    )
    assert code == 0
    if legacy_expected:
        assert "stage" not in result["expected"]
        assert "first_severity" not in result
        assert "impact" not in result["ai_runs"][0]
    else:
        assert result["expected"]["stage"] == "normal"
        assert result["expected"]["expected_first_severity"] is None
        assert result["first_severity"]["status"] == status
        assert result["ai_runs"][0]["impact"]["status"] == "skipped"
