from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "kubeguardian"


def _docs(name):
    return list(yaml.safe_load_all((DEPLOY / name).read_text(encoding="utf-8")))


def _kind(docs, kind):
    return next(d for d in docs if d["kind"] == kind)


def _cronjob():
    return _kind(_docs("cronjob.yaml"), "CronJob")


def _pod():
    return _cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def test_cronjob_runs_every_five_minutes_without_overlap():
    spec = _cronjob()["spec"]
    assert spec["schedule"] == "*/5 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["startingDeadlineSeconds"] == 120
    assert spec["successfulJobsHistoryLimit"] == 3
    assert spec["failedJobsHistoryLimit"] == 3


def test_job_does_not_retry_and_ends_before_next_run():
    job = _cronjob()["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0
    assert job["activeDeadlineSeconds"] == 270
    assert job["ttlSecondsAfterFinished"] == 3600
    assert job["template"]["spec"]["restartPolicy"] == "Never"


def test_container_runs_check_with_schedule_trigger_on_pvc_db():
    c = _pod()["containers"][0]
    assert c["image"] == "kubeguardian:local"
    assert c["imagePullPolicy"] == "Never"
    assert c["command"] == [
        "python",
        "/kg/app/cli.py",
        "check",
        "--trigger",
        "schedule",
        "--db",
        "/data/kubeguardian.db",
    ]


def test_data_volume_is_the_shared_pvc():
    pod = _pod()
    vol = next(v for v in pod["volumes"] if v["name"] == "data")
    assert vol["persistentVolumeClaim"]["claimName"] == "kubeguardian-data"
    pvc = _kind(_docs("cronjob.yaml"), "PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == "kubeguardian-data"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert "storageClassName" not in pvc["spec"]


def test_pod_is_locked_down_like_the_runner():
    pod = _pod()
    c = pod["containers"][0]
    assert pod["serviceAccountName"] == "kubeguardian"
    assert pod["securityContext"]["fsGroup"] == 10001
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == 10001
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}


def test_env_has_only_paths_and_addresses_and_secrets_come_from_envfrom():
    c = _pod()["containers"][0]
    assert {e["name"] for e in c["env"]} == {
        "KG_CONFIG_PATH",
        "KG_ACCOUNTS_DIR",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "SSL_CERT_FILE",
    }
    assert all("valueFrom" not in e for e in c["env"])
    assert c["envFrom"] == [{"secretRef": {"name": "kubeguardian-env"}}]


def test_test_user_secret_is_optional():
    vol = next(v for v in _pod()["volumes"] if v["name"] == "test-user")
    assert vol["secret"]["optional"] is True
