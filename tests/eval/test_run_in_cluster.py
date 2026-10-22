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


def _check_payload(run_id=1, detected=True, failed=None):
    signals = [SIGNAL] if detected else []
    return {
        "run_id": run_id,
        "gate_path": "A" if detected else "C",
        "gate": {"path": "A" if detected else "C", "reason": "적발: S01" if detected else ""},
        "signals": signals,
        "snapshot_failed": failed or {},
    }


class FakeKubectl:
    """kubectl 호출을 흉내 낸다. pods는 호출마다 돌려줄 Pod 이름 목록을 소진한다."""

    def __init__(self, pods=("kg-runner-aaa",), execs=()):
        self.pods = list(pods)
        self.execs = list(execs)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
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
    assert exec_cmd[exec_cmd.index("--") + 1 :][:3] == ["python", "-m", "runner_step"]
    assert "/data/20261022T000000Z-A1/kg.db" in exec_cmd


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

    def start(self):
        return self.pod

    def check(self):
        self.calls += 1
        if self.fail_at == self.calls:
            raise RemoteError("runner Pod가 바뀌었다 (a → b)")
        return self.outcomes(self.calls)

    def fetch(self, dest):
        self.fetched = dest


def _run_in_cluster(tmp_path, monkeypatch, scenario, fail_at=None):
    import run as run_mod

    cfg = tmp_path / "kg.yaml"
    cfg.write_text("target: {namespace: dev}\ncore_paths: {chat: [gateway, agent]}\n")
    monkeypatch.setattr(run_mod, "ROOT", tmp_path)
    monkeypatch.setattr(run_mod, "HISTORY", tmp_path / "history")
    monkeypatch.setattr(run_mod, "until_ok", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        run_mod,
        "sh",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        run_mod,
        "K8sClient",
        type("NoLocalK8s", (), {"from_config": staticmethod(_boom)}),
    )
    stub = StubRemote(
        "x", fail_at=fail_at, outcomes=lambda n: _stub_outcome(detected=(scenario != "H0"))
    )
    monkeypatch.setattr(run_mod, "RemoteRunner", lambda name: stub)
    code = run_mod.run(scenario, str(cfg), 1, 300, 2700, "rules", in_cluster=True)
    result = json.loads(next((tmp_path / "history").glob("*/result.json")).read_text())
    return code, result, stub


def _boom(*a, **k):
    raise AssertionError("--in-cluster는 Mac에서 클러스터를 읽지 않는다")


def _stub_outcome(detected):
    return _runner(FakeKubectl(execs=[_check_payload(1, detected=detected)])).check()


def test_run_in_cluster_h0_passes_and_fetches_db(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0")

    assert code == 0, result
    assert result["in_cluster"] == {"pod": "kg-runner-aaa"}
    assert result["checks"][0]["n"] == 1
    assert stub.fetched is not None and stub.fetched.name.endswith("-H0")


def test_run_in_cluster_pod_error_is_invalid_and_still_fetches(tmp_path, monkeypatch):
    code, result, stub = _run_in_cluster(tmp_path, monkeypatch, "H0", fail_at=1)

    assert code == 2
    assert "RemoteError" in result["invalid"] and "Pod" in result["invalid"]
    assert result["recovered"] is True
