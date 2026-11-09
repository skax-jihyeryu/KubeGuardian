"""Local-test-only C8: change one non-secret LLM deployment name and restore it."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NS = os.environ.get("KG_NAMESPACE", "dev")
STATE = Path(__file__).parent / ".state" / f"{NS}-deployment.json"
BAD_NAME = "gpt-4.1-next"


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def kubectl(*args):
    return run(["kubectl", "-n", NS, *args])


def sql(statement):
    return run(
        [
            "kubectl",
            "-n",
            NS,
            "exec",
            "-i",
            "deployment/postgres",
            "--",
            "sh",
            "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ],
        input=statement,
    )


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def row():
    rows = sql(
        "SELECT json_build_object('id',id,'provider',provider,'model_name',model_name, "
        "'deployment_name',deployment_name) FROM llm_deployment "
        "WHERE is_active=true AND model_name='gpt-4.1';"
    )
    parsed = [json.loads(line) for line in rows.splitlines() if line.strip()]
    if len(parsed) != 1 or parsed[0]["provider"] != "azure-openai":
        raise RuntimeError("Expected exactly one active Azure gpt-4.1 test deployment")
    return parsed[0]


def replace(saved, old, new):
    result = sql(
        f"UPDATE llm_deployment SET deployment_name={quote(new)} "
        f"WHERE id={int(saved['id'])} AND deployment_name={quote(old)} RETURNING id;"
    )
    if result.strip() != str(saved["id"]):
        raise RuntimeError("Deployment changed concurrently; original state retained for review")


def verify():
    current = row()
    if current["deployment_name"] != BAD_NAME or not STATE.exists():
        raise RuntimeError("C8 fault is not injected")
    for app in ("llm-gateway", "agent", "gateway", "front-chat"):
        pods = json.loads(kubectl("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]
        if not pods or any(
            not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p["status"].get("conditions", [])
            )
            for p in pods
        ):
            raise RuntimeError(f"{app} Pod must exist and be Ready; this is not a Pod health fault")
    smoke = subprocess.run(
        ["bash", str(ROOT / "scripts/target/smoke.sh")], cwd=ROOT, capture_output=True, text=True
    )
    output = smoke.stdout + smoke.stderr
    if smoke.returncode == 0 or "오류가 발생했습니다" not in output:
        raise RuntimeError("Expected actual chat failure; response omitted")
    injected_at = json.loads(STATE.read_text())["injected_at"]
    # llm-gateway 파드 로그는 SQLAlchemy가 log_data를 잘라 403 근거가 남지 않는다.
    # 같은 근거는 게이트웨이가 서버 측에 기록한 llm_audit_log에 온전히 있다.
    evidence = sql(
        "SELECT count(*) FROM llm_audit_log "
        f"WHERE deployment_name={quote(BAD_NAME)} AND response_status='error' "
        f"AND create_dt >= {quote(injected_at)}::timestamptz "
        "AND log_data::text LIKE '%deployment_not_allowed%' "
        "AND log_data::text LIKE '%403: Azure OpenAI%';"
    ).strip()
    if not evidence.isdigit() or int(evidence) < 1:
        raise RuntimeError("Server-side 403 deployment_not_allowed evidence missing")
    print(
        json.dumps(
            {
                "valid": True,
                "target_pods_ready": True,
                "chat_failed": True,
                "upstream_error": "403 deployment_not_allowed",
                "server_log_matches": True,
            }
        )
    )


def main(action):
    if NS != "dev":
        raise RuntimeError("This rehearsal is restricted to dev")
    if action == "inject":
        if STATE.exists():
            raise RuntimeError("Saved state exists: restore before another injection")
        saved = row()
        if saved["deployment_name"] == BAD_NAME:
            raise RuntimeError("Deployment already contains fault without saved original")
        saved["injected_at"] = datetime.now(UTC).isoformat()
        STATE.parent.mkdir(exist_ok=True)
        with STATE.open("x") as f:
            json.dump(saved, f)
        replace(saved, saved["deployment_name"], BAD_NAME)
        print("C8 injected: test deployment_name changed; original saved; credentials unchanged")
    elif action == "reset":
        if not STATE.exists():
            print("No C8 saved state; nothing changed")
            return
        saved = json.loads(STATE.read_text())
        current = row()
        if current["id"] != saved["id"]:
            raise RuntimeError("Deployment ID changed; refusing restore")
        if current["deployment_name"] != saved["deployment_name"]:
            replace(saved, BAD_NAME, saved["deployment_name"])
        if row()["deployment_name"] != saved["deployment_name"]:
            raise RuntimeError("Restore verification failed")
        STATE.unlink()
        print("C8 restored: original deployment_name verified")
    elif action == "verify":
        verify()
    else:
        raise RuntimeError("Expected inject, reset or verify")


if __name__ == "__main__":
    main(sys.argv[1])
