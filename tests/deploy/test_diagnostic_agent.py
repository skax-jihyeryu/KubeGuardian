from pathlib import Path

from tests.deploy.test_manifests import _docs, _kind, _pod

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"


def _deployment():
    return _kind(_docs("diagnostic-agent.yaml"), "Deployment")


def _containers():
    pod = _deployment()["spec"]["template"]["spec"]
    return {c["name"]: c for c in pod["containers"]}


def test_single_replica_replaced_without_overlap():
    spec = _deployment()["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}


def test_pod_runs_worker_web_and_listener_containers():
    assert list(_containers()) == ["worker", "web", "listener"]


def test_runs_worker_on_the_same_db_as_the_cronjob():
    pod = _deployment()["spec"]["template"]["spec"]
    worker = _containers()["worker"]
    assert worker["command"][2:] == ["worker", "--db", "/data/kubeguardian.db"]
    data = next(v for v in pod["volumes"] if v["name"] == "data")
    cron_data = next(v for v in _pod()["volumes"] if v["name"] == "data")
    assert data == cron_data  # 같은 PVC
    assert _pod()["containers"][0]["command"][-1] == "/data/kubeguardian.db"


def test_same_secrets_env_and_hardening_as_the_cronjob():
    pod = _deployment()["spec"]["template"]["spec"]
    worker, [cron] = _containers()["worker"], _pod()["containers"]
    assert pod["serviceAccountName"] == "kubeguardian"
    for key in ("envFrom", "env", "securityContext", "volumeMounts"):
        assert worker[key] == cron[key], key


# ── web 컨테이너 (보고서 화면, #23) ──


def test_web_serves_the_screens_on_8080_from_the_same_image_and_db():
    web, worker = _containers()["web"], _containers()["worker"]
    assert web["image"] == worker["image"] == "kubeguardian:local"
    assert web["imagePullPolicy"] == "Never"
    assert web["command"] == ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
    assert web["ports"] == [{"name": "web", "containerPort": 8080}]
    assert {"name": "KG_DB_PATH", "value": "/data/kubeguardian.db"} in web["env"]


def test_web_has_the_same_env_secrets_mounts_and_hardening_as_the_worker():
    web, worker = _containers()["web"], _containers()["worker"]
    for key in ("envFrom", "securityContext", "volumeMounts"):
        assert web[key] == worker[key], key
    extra = [e for e in web["env"] if e not in worker["env"]]
    assert extra == [{"name": "KG_DB_PATH", "value": "/data/kubeguardian.db"}]
    assert all(e in web["env"] for e in worker["env"])


def test_web_is_ready_only_when_healthz_answers():
    web = _containers()["web"]
    assert web["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    assert web["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}


# ── listener 컨테이너 (Slack 확인 버튼, #156) ──


def test_listener_runs_slack_listen_on_the_same_db():
    listener, worker = _containers()["listener"], _containers()["worker"]
    assert listener["image"] == worker["image"]
    assert listener["command"][2:] == ["slack-listen", "--db", "/data/kubeguardian.db"]


def test_listener_has_the_same_env_secrets_mounts_and_hardening_as_the_worker():
    listener, worker = _containers()["listener"], _containers()["worker"]
    for key in ("envFrom", "env", "securityContext", "volumeMounts"):
        assert listener[key] == worker[key], key


def test_listener_is_small():
    resources = _containers()["listener"]["resources"]
    assert resources == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    }


def test_service_is_cluster_ip_without_ingress():
    docs = _docs("diagnostic-agent.yaml")
    [svc] = [d for d in docs if d["kind"] == "Service"]
    assert svc["metadata"]["name"] == "diagnostic-agent"
    assert svc["metadata"]["namespace"] == "kubeguardian"
    spec = svc["spec"]
    assert spec.get("type", "ClusterIP") == "ClusterIP"
    assert spec["selector"] == {"app": "diagnostic-agent"}
    assert spec["ports"] == [{"name": "web", "port": 8080, "targetPort": "web"}]
    assert not [d for d in docs if d["kind"] in ("Ingress", "HTTPRoute")]


def test_make_kg_web_port_forwards_the_service():
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "kg-web:" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("\t") and "port-forward" in ln)
    assert "service/diagnostic-agent" in line and "8080:8080" in line and "-n $(KG_NS)" in line
    phony = next(ln for ln in text.splitlines() if ln.startswith(".PHONY"))
    assert "kg-web" in phony.split()
