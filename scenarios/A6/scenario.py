"""Remove the local test gateway's login upstream setting and restore it."""

import base64
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STATE = Path(__file__).parent / ".state" / "dev-gateway.json"
NAME = "ADMIN_SERVICE_URL"
FALLBACK = "http://localhost:8001"
ORIGINAL = {"name": NAME, "value": "http://admin:8001"}
DEPLOY = "deployment/gateway"
ENV_PATH = "/spec/template/spec/containers/0/env"


def run(*args, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("kubectl operation failed; output omitted")
    return result.stdout


def guard():
    if os.environ.get("KG_NAMESPACE", "dev") != "dev":
        raise RuntimeError("A6 requires the dev test namespace")
    if run("kubectl", "config", "current-context").strip() != "minikube":
        raise RuntimeError("A6 requires the minikube context")
    field = r'{.clusters[0].cluster.server}{"\n"}{.clusters[0].cluster.certificate-authority-data}'
    endpoint, _, ca_data = run(
        "kubectl",
        "config",
        "view",
        "--context=minikube",
        "--minify",
        "--flatten",
        "--raw",
        "-o",
        f"jsonpath={field}",
    ).partition("\n")
    local_ca = Path.home() / ".minikube" / "ca.crt"
    if not endpoint.startswith("https://") or base64.b64decode(ca_data) != local_ca.read_bytes():
        raise RuntimeError("The context must use the local Minikube certificate authority")
    return endpoint


def kubectl(*args, patch_body=None):
    return run("kubectl", "--context=minikube", "-n", "dev", *args, input_text=patch_body)


def deployment():
    obj = json.loads(kubectl("get", DEPLOY, "-o", "json"))
    meta = obj["metadata"]
    template = obj["spec"]["template"]
    pod = template["spec"]
    containers = pod["containers"]
    if (
        meta["name"] != "gateway"
        or meta["namespace"] != "dev"
        or template["metadata"].get("labels", {}).get("app") != "gateway"
        or pod.get("hostNetwork")
        or pod.get("initContainers")
        or len(containers) != 1
        or containers[0]["name"] != "gateway"
        or containers[0]["image"] != "kg-target/gateway:local"
        or containers[0].get("command")
        or containers[0].get("args")
    ):
        raise RuntimeError("Expected the local overlay's single-container gateway Deployment")
    return meta, containers[0]


def check_fallback(container):
    for source in container.get("envFrom", []):
        prefix = source.get("prefix", "")
        if not NAME.lower().startswith(prefix.lower()):
            continue
        if "configMapRef" in source:
            kind, ref = "configmap", source["configMapRef"]
        elif "secretRef" in source:
            kind, ref = "secret", source["secretRef"]
        else:
            raise RuntimeError("Unsupported gateway envFrom source")
        # Ask for keys only. Never emit values from envFrom or kubectl errors.
        keys = kubectl(
            "get",
            kind,
            ref["name"],
            f"--ignore-not-found={str(ref.get('optional', False)).lower()}",
            "-o",
            "go-template={{range $key, $value := .data}}{{println $key}}{{end}}",
        )
        if any(f"{prefix}{key}".lower() == NAME.lower() for key in keys.splitlines()):
            raise RuntimeError("An envFrom source also supplies ADMIN_SERVICE_URL")
    # Check the running local image. Removing this key in a child process also
    # checks for .env overrides without modifying the Deployment or the Pod.
    program = (
        "import os; from core.config import Settings; "
        f"original = os.environ.pop({NAME!r}, None); "
        f"print(original == {ORIGINAL['value']!r} and "
        f"Settings().{NAME} == {FALLBACK!r})"
    )
    result = kubectl(
        "exec",
        DEPLOY,
        "-c",
        "gateway",
        "--",
        "/app/.venv/bin/python",
        "-c",
        program,
    )
    if result.strip() != "True":
        raise RuntimeError("The running gateway must have the F1 URL and the localhost fallback")


def digest(env):
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()


def save(state):
    with tempfile.NamedTemporaryFile(dir=STATE.parent, delete=False, mode="w") as file:
        temporary = Path(file.name)
        try:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(STATE)
        finally:
            temporary.unlink(missing_ok=True)


def change_env(meta, operation):
    patch = [
        {"op": "test", "path": f"/metadata/{key}", "value": meta[key]}
        for key in ("uid", "resourceVersion")
    ]
    patch.append(operation)
    kubectl("patch", DEPLOY, "--type=json", "--patch-file=/dev/stdin", patch_body=json.dumps(patch))


def rollout():
    kubectl("rollout", "status", DEPLOY, "--timeout=180s")


def apply(action, endpoint):
    if action == "reset" and not STATE.exists():
        print("A6 주입 기록 없음")
        return
    if action == "inject" and STATE.exists():
        raise RuntimeError("A6 state already exists; run reset.sh first")
    meta, gateway = deployment()
    env = gateway.get("env", [])
    if action == "inject":
        indices = [i for i, entry in enumerate(env) if entry["name"].lower() == NAME.lower()]
        if len(indices) != 1 or env[indices[0]] != ORIGINAL or len(env) < 2:
            raise RuntimeError("Expected one literal F1 ADMIN_SERVICE_URL in the gateway env list")
        index = indices[0]
        check_fallback(gateway)
        save(
            {
                "endpoint": endpoint,
                "uid": meta["uid"],
                "index": index,
                "entry": env[index],
                "before": digest(env),
                "after": digest(env[:index] + env[index + 1 :]),
            }
        )
        change_env(meta, {"op": "remove", "path": f"{ENV_PATH}/{index}"})
        rollout()
        print("A6 주입: dev/Deployment/gateway에서 ADMIN_SERVICE_URL 제거")
        return
    if not STATE.exists():
        raise RuntimeError("A6 injection state is missing")
    saved = json.loads(STATE.read_text())
    if saved["endpoint"] != endpoint or saved["uid"] != meta["uid"]:
        raise RuntimeError("The cluster or gateway Deployment changed; state retained")
    current = digest(env)
    if action == "verify":
        if current != saved["after"]:
            raise RuntimeError("The gateway env does not match the A6 injected state")
        return
    if current == saved["after"]:
        change_env(
            meta,
            {
                "op": "add",
                "path": f"{ENV_PATH}/{saved['index']}",
                "value": saved["entry"],
            },
        )
    elif current != saved["before"]:
        raise RuntimeError("The gateway env changed concurrently; state retained")
    rollout()
    restored, gateway = deployment()
    if restored["uid"] != meta["uid"] or digest(gateway.get("env", [])) != saved["before"]:
        raise RuntimeError("Restore verification failed; state retained")
    STATE.unlink()
    print("A6 원복: ADMIN_SERVICE_URL 복구 확인")


def main(action):
    endpoint = guard()
    STATE.parent.mkdir(mode=0o700, exist_ok=True)
    with (STATE.parent / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        apply(action, endpoint)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"inject", "reset", "verify"}:
            raise RuntimeError("Expected inject, reset, or verify")
        main(sys.argv[1])
    except RuntimeError as exc:
        sys.exit(f"A6: {exc}")
    except (OSError, KeyError, TypeError, ValueError):
        sys.exit("A6: cannot read the local Minikube target or saved state")
