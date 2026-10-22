"""평가 러너 --in-cluster: runner Pod에 `kubectl exec`로 점검·조사를 시킨다 (이슈 #72).

주입·원복·채점은 Mac에서 하고, 점검(반복 점검 포함)과 AI 조사만 Pod에서 실행한다.
Pod 안 진입점은 runner_step.py이고 점검 DB는 Pod의 /data/<실행 이름>/kg.db에 쌓인다 (emptyDir).
Pod가 바뀌면 DB가 사라져 지속 시간 규칙이 틀어지므로 실행 시작 때 Pod 이름을 기록하고,
호출할 때마다 같은 Pod인지 확인해 다르면 RemoteError를 낸다 (러너는 무효 처리한다).
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.guard.redact import redact_text
from runner_step import scrub
from service.agent.base import AgentResult
from service.model import GateResult, Signal

NAMESPACE = "kubeguardian"
SELECTOR = "app=kg-runner"
EXEC_TIMEOUT_S = 1800
_KUBECTL_MARGIN_S = 30  # Pod 안 timeout이 먼저 끝내도록 kubectl 쪽은 조금 더 준다
_STDERR_TAIL = 800
_POD_TIMEOUT_EXIT = 124  # coreutils timeout이 시간 초과로 끊을 때의 종료 코드

Run = Callable[..., subprocess.CompletedProcess]


class RemoteError(Exception):
    """runner Pod 호출 실패. 메시지는 사유(마스킹됨)다."""


class RemoteTimeout(RemoteError):
    """kubectl 호출이 시간 안에 끝나지 않았다."""


class RemoteRunner:
    def __init__(self, run_name: str, *, run: Run = subprocess.run):
        self._run = run
        self.run_name = run_name
        self.pod: str | None = None
        # exec이 시간 초과로 끊기면 Pod 안 점검이 DB를 쓰다 말았을 수 있다
        self.db_incomplete = False

    @property
    def db_path(self) -> str:
        return f"/data/{self.run_name}/kg.db"

    def start(self) -> str:
        """현재 runner Pod를 기록한다. 이후 호출은 이 Pod에만 보낸다."""
        self.pod = self._current_pod()
        return self.pod

    def describe(self) -> dict[str, str]:
        """결과에 남길 실행 환경: runner 이미지 ID와 점검 설정 ConfigMap의 resourceVersion."""
        pod = self._same_pod()
        image = self._kubectl(
            "get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[0].imageID}"
        )
        cm = self._kubectl(
            "get",
            "configmap",
            "kubeguardian-config",
            "-o",
            "jsonpath={.metadata.resourceVersion}",
        )
        for r in (image, cm):
            if r.returncode != 0:
                raise RemoteError(f"실행 환경 조회 실패 (exit {r.returncode}): {_tail(r.stderr)}")
        return {"image_id": image.stdout.strip(), "config_resource_version": cm.stdout.strip()}

    def check(self) -> Any:
        """점검 한 번. check_repeatedly가 받는 outcome 모양(run_id·signals·gate·snapshot.failed)."""
        data = self._exec("check", "--db", self.db_path)
        return SimpleNamespace(
            run_id=data["run_id"],
            signals=[Signal.model_validate(s) for s in data["signals"]],
            gate=GateResult.model_validate(data["gate"]),
            snapshot=SimpleNamespace(failed=data["snapshot_failed"]),
        )

    def investigate(self, check_run_id: int, run_id: str) -> tuple[AgentResult | None, str | None]:
        """조사 한 번. 게이트가 닫혀 있으면 (None, None). 두 번째 값은 Pod 안 추적의 trace_id."""
        data = self._exec(
            "investigate",
            "--db",
            self.db_path,
            "--check-run-id",
            str(check_run_id),
            "--run-id",
            run_id,
        )
        if data.get("skipped"):
            return None, None
        trace_id = data.pop("trace_id", None)
        return AgentResult.model_validate(data), trace_id

    def fetch(self, dest: Path) -> None:
        """Pod의 점검 DB를 history 디렉터리로 가져온다."""
        pod = self._same_pod()
        r = self._kubectl(
            "cp", f"{NAMESPACE}/{pod}:{self.db_path}", str(dest / "kg.db"), timeout=300
        )
        if r.returncode != 0:
            raise RemoteError(f"kubectl cp 실패 (exit {r.returncode}): {_tail(r.stderr)}")

    def _current_pod(self) -> str:
        r = self._kubectl(
            "get",
            "pods",
            "-l",
            SELECTOR,
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        names = r.stdout.split()
        if r.returncode != 0 or len(names) != 1:
            raise RemoteError(
                f"runner Pod를 하나로 찾지 못함 (exit {r.returncode}, {names}): {_tail(r.stderr)}"
                " — make kg-up을 했는지 확인한다"
            )
        return names[0]

    def _same_pod(self) -> str:
        if self.pod is None:
            raise RemoteError("start()를 먼저 부른다")
        current = self._current_pod()
        if current != self.pod:
            raise RemoteError(
                f"runner Pod가 바뀌었다 ({self.pod} → {current}). 점검 DB가 사라져 이어 볼 수 없다"
            )
        return self.pod

    def _exec(self, *args: str) -> dict:
        pod = self._same_pod()
        try:
            r = self._kubectl(
                "exec",
                pod,
                "--",
                "timeout",
                str(EXEC_TIMEOUT_S),
                "python",
                "-m",
                "runner_step",
                *args,
                "--json",
                timeout=EXEC_TIMEOUT_S + _KUBECTL_MARGIN_S,
            )
        except RemoteTimeout:
            self.db_incomplete = True
            raise
        if r.returncode == _POD_TIMEOUT_EXIT:
            self.db_incomplete = True
        if r.returncode != 0:
            raise RemoteError(f"exec 실패 (exit {r.returncode}): {_tail(r.stderr)}")
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        try:
            data = json.loads(lines[-1])
        except (IndexError, ValueError) as e:
            raise RemoteError(f"exec 출력이 JSON이 아니다: {_tail(r.stdout)}") from e
        if not isinstance(data, dict):
            raise RemoteError(f"exec 출력이 JSON 객체가 아니다: {_tail(r.stdout)}")
        return scrub(data)

    def _kubectl(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = ["kubectl", "-n", NAMESPACE, *args]
        try:
            return self._run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RemoteTimeout(f"kubectl 시간 초과 ({timeout}초): {args[0]}") from e


def _tail(text: str) -> str:
    return redact_text(text.strip()[-_STDERR_TAIL:])
