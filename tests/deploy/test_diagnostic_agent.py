from tests.deploy.test_manifests import _docs, _kind, _pod


def _deployment():
    return _kind(_docs("diagnostic-agent.yaml"), "Deployment")


def test_single_worker_replaced_without_overlap():
    spec = _deployment()["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}


def test_runs_worker_on_the_same_db_as_the_cronjob():
    pod = _deployment()["spec"]["template"]["spec"]
    [c] = pod["containers"]
    assert c["command"][2:] == ["worker", "--db", "/data/kubeguardian.db"]
    data = next(v for v in pod["volumes"] if v["name"] == "data")
    cron_data = next(v for v in _pod()["volumes"] if v["name"] == "data")
    assert data == cron_data  # 같은 PVC
    assert _pod()["containers"][0]["command"][-1] == "/data/kubeguardian.db"


def test_same_secrets_env_and_hardening_as_the_cronjob():
    pod = _deployment()["spec"]["template"]["spec"]
    [c], [cron] = pod["containers"], _pod()["containers"]
    assert pod["serviceAccountName"] == "kubeguardian"
    for key in ("envFrom", "env", "securityContext", "volumeMounts"):
        assert c[key] == cron[key], key
