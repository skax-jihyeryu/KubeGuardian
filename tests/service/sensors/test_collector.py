from datetime import UTC, datetime

import pytest

from core.error.service_exception import ErrorCode, ServiceException
from core.k8s import K8sCallError
from service.sensors.collector import (
    COLLECTED_KINDS,
    collect,
    fetch_logs,
    log_evidence,
    resource_evidence,
)

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


def _pod(name="agent-7c9f-x2k", node="minikube", env=None):
    return {
        "metadata": {
            "name": name,
            "namespace": "dev",
            "uid": "u-1",
            "labels": {"app": "agent"},
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"},
            "managedFields": [{"manager": "kubectl"}],
            "ownerReferences": [
                {"kind": "ReplicaSet", "name": "agent-7c9f", "uid": "rs-1", "controller": True}
            ],
            "creationTimestamp": "2026-10-20T00:00:00Z",
        },
        "spec": {
            "nodeName": node,
            "serviceAccountName": "default",
            "containers": [
                {
                    "name": "agent",
                    "image": "agent:1.0",
                    "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                    "env": env
                    if env is not None
                    else [
                        {"name": "LLM_GATEWAY_SERVICE_URL", "value": "http://llm-gateway:8000"},
                        {"name": "DB_PASSWORD", "value": "hunter2"},
                        {
                            "name": "API_KEY",
                            "valueFrom": {"secretKeyRef": {"name": "agent-secret", "key": "k"}},
                        },
                    ],
                    "envFrom": [{"configMapRef": {"name": "agent-config"}}],
                    "resources": {"limits": {"memory": "512Mi"}},
                    "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}},
                    "volumeMounts": [{"name": "cfg", "mountPath": "/cfg"}],
                    "terminationMessagePath": "/dev/termination-log",
                }
            ],
            "volumes": [
                {"name": "cfg", "configMap": {"name": "agent-config"}},
                {"name": "kube-api-access", "projected": {"sources": []}},
            ],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.0.0.5",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "agent",
                    "ready": True,
                    "restartCount": 2,
                    "image": "agent:1.0",
                    "imageID": "docker://sha256:abc",
                    "state": {"running": {"startedAt": "2026-10-20T00:01:00Z"}},
                    "lastState": {
                        "terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "x"}
                    },
                }
            ],
        },
    }


def _node(name):
    return {
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}},
        "spec": {"unschedulable": True, "podCIDR": "10.0.0.0/24"},
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "reason": "KubeletReady"}],
            "images": [{"names": ["big"]}],
        },
    }


class FakeReader:
    """Kubernetes API 응답을 흉내 낸다. kind → 객체 목록 또는 예외."""

    namespace = "dev"

    def __init__(self, responses=None, logs=None):
        self.responses = {
            "Node": [_node("minikube"), _node("other-node")],
            "Pod": [_pod()],
            "Service": [],
            "EndpointSlice": [],
            "Deployment": [],
            "ReplicaSet": [],
            "ConfigMap": [],
            "Secret": [],
            "Event": [],
        }
        self.responses.update(responses or {})
        self.logs = logs or {}
        self.log_calls = []

    def list(self, kind):
        assert kind != "Node", "Node는 list_nodes로 읽는다"
        return self._respond(kind)

    def list_nodes(self):
        return self._respond("Node")

    def _respond(self, kind):
        value = self.responses[kind]
        if isinstance(value, Exception):
            raise value
        return value

    def read_log(self, pod, container, *, previous, tail_lines):
        self.log_calls.append((pod, container, previous, tail_lines))
        value = self.logs[(pod, container, previous)]
        if isinstance(value, Exception):
            raise value
        return value


def test_all_kinds_are_collected_with_kubernetes_kind_names():
    assert set(COLLECTED_KINDS) == {
        "Node",
        "Pod",
        "Service",
        "EndpointSlice",
        "Deployment",
        "ReplicaSet",
        "ConfigMap",
        "Secret",
        "Event",
    }

    snapshot = collect(FakeReader(), now=NOW)

    assert snapshot.namespace == "dev"
    assert snapshot.collected_at == NOW
    assert set(snapshot.resources) == set(COLLECTED_KINDS)
    assert snapshot.failed == {}


def test_empty_kind_is_an_empty_list_not_unknown():
    snapshot = collect(FakeReader(), now=NOW)

    assert snapshot.items("Service") == []


def test_failed_kind_goes_to_failed_with_reason_and_is_unknown():
    reader = FakeReader({"Secret": K8sCallError("403 Forbidden")})

    snapshot = collect(reader, now=NOW)

    assert snapshot.failed == {"Secret": "403 Forbidden"}
    assert "Secret" not in snapshot.resources
    assert snapshot.items("Secret") is None
    assert snapshot.items("Pod") is not None


def test_failure_reason_is_redacted():
    reader = FakeReader({"Event": K8sCallError("connect http://u:hunter2@api failed")})

    snapshot = collect(reader, now=NOW)

    assert "hunter2" not in snapshot.failed["Event"]


def test_only_nodes_hosting_target_pods_are_kept():
    snapshot = collect(FakeReader(), now=NOW)

    assert [n["metadata"]["name"] for n in snapshot.items("Node")] == ["minikube"]


def test_nodes_are_unknown_when_pods_failed():
    snapshot = collect(FakeReader({"Pod": K8sCallError("500 Internal Server Error")}), now=NOW)

    assert snapshot.items("Pod") is None
    assert snapshot.items("Node") is None
    assert "Pod" in snapshot.failed["Node"]


def test_pod_is_normalized_to_fields_detectors_need():
    pod = collect(FakeReader(), now=NOW).items("Pod")[0]

    assert pod["kind"] == "Pod"
    assert pod["metadata"] == {
        "name": "agent-7c9f-x2k",
        "namespace": "dev",
        "uid": "u-1",
        "labels": {"app": "agent"},
        "ownerReferences": [
            {"kind": "ReplicaSet", "name": "agent-7c9f", "uid": "rs-1", "controller": True}
        ],
        "creationTimestamp": "2026-10-20T00:00:00Z",
    }
    container = pod["spec"]["containers"][0]
    assert set(container) == {
        "name",
        "image",
        "ports",
        "env",
        "envFrom",
        "resources",
        "readinessProbe",
    }
    assert pod["spec"]["nodeName"] == "minikube"
    assert pod["spec"]["volumes"] == [{"name": "cfg", "configMap": {"name": "agent-config"}}]
    status = pod["status"]["containerStatuses"][0]
    assert status["restartCount"] == 2
    assert status["lastState"]["terminated"]["reason"] == "OOMKilled"
    assert "imageID" not in status
    assert pod["status"]["conditions"] == [{"type": "Ready", "status": "True"}]


def test_env_values_are_redacted_and_references_kept():
    env = collect(FakeReader(), now=NOW).items("Pod")[0]["spec"]["containers"][0]["env"]

    assert env[0] == {"name": "LLM_GATEWAY_SERVICE_URL", "value": "http://llm-gateway:8000"}
    assert env[1] == {"name": "DB_PASSWORD", "value": "***"}
    assert env[2]["valueFrom"] == {"secretKeyRef": {"name": "agent-secret", "key": "k"}}


def test_secret_keeps_metadata_and_key_names_only():
    secret = {
        "metadata": {"name": "agent-secret", "namespace": "dev", "labels": {"app": "agent"}},
        "type": "Opaque",
        "data": {"API_KEY": "c2VjcmV0", "DB_URL": "cG9zdGdyZXM="},
        "stringData": {"EXTRA": "plain-value"},
    }

    normalized = collect(FakeReader({"Secret": [secret]}), now=NOW).items("Secret")[0]

    assert normalized == {
        "kind": "Secret",
        "metadata": {"name": "agent-secret", "namespace": "dev", "labels": {"app": "agent"}},
        "type": "Opaque",
        "keys": ["API_KEY", "DB_URL", "EXTRA"],
    }
    assert "c2VjcmV0" not in str(normalized)
    assert "plain-value" not in str(normalized)


def test_configmap_keeps_keys_and_redacted_values():
    cm = {
        "metadata": {"name": "agent-config", "namespace": "dev"},
        "data": {"GATEWAY_URL": "http://gateway:80", "db_password": "hunter2"},
        "binaryData": {"cert.der": "AAAA"},
    }

    normalized = collect(FakeReader({"ConfigMap": [cm]}), now=NOW).items("ConfigMap")[0]

    assert normalized["data"] == {"GATEWAY_URL": "http://gateway:80", "db_password": "***"}
    assert normalized["binaryDataKeys"] == ["cert.der"]
    assert "AAAA" not in str(normalized)


def test_event_message_is_redacted():
    event = {
        "metadata": {"name": "agent.1", "namespace": "dev"},
        "involvedObject": {"kind": "Pod", "name": "agent-7c9f-x2k", "namespace": "dev"},
        "reason": "Failed",
        "message": "pull failed: Authorization: Bearer abcdefghijklmnop",
        "type": "Warning",
        "count": 3,
        "firstTimestamp": "2026-10-20T00:00:00Z",
        "lastTimestamp": "2026-10-20T00:05:00Z",
        "source": {"component": "kubelet"},
    }

    normalized = collect(FakeReader({"Event": [event]}), now=NOW).items("Event")[0]

    assert "abcdefghijklmnop" not in normalized["message"]
    assert normalized["involvedObject"] == {
        "kind": "Pod",
        "name": "agent-7c9f-x2k",
        "namespace": "dev",
    }
    assert normalized["reason"] == "Failed"
    assert normalized["count"] == 3


def test_service_endpointslice_deployment_replicaset_and_node_are_normalized():
    service = {
        "metadata": {"name": "agent", "namespace": "dev"},
        "spec": {
            "type": "ClusterIP",
            "clusterIP": "10.96.0.10",
            "selector": {"app": "agent"},
            "ports": [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}],
            "sessionAffinity": "None",
        },
        "status": {"loadBalancer": {}},
    }
    slice_ = {
        "metadata": {
            "name": "agent-abcde",
            "namespace": "dev",
            "labels": {"kubernetes.io/service-name": "agent"},
        },
        "addressType": "IPv4",
        "endpoints": [
            {
                "addresses": ["10.0.0.5"],
                "conditions": {"ready": False, "serving": False, "terminating": False},
                "targetRef": {"kind": "Pod", "name": "agent-7c9f-x2k", "namespace": "dev"},
                "nodeName": "minikube",
                "zone": "z",
            }
        ],
        "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
    }
    deployment = {
        "metadata": {"name": "agent", "namespace": "dev", "generation": 3},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "agent"}},
            "progressDeadlineSeconds": 600,
            "revisionHistoryLimit": 10,
            "template": {"metadata": {"labels": {"app": "agent"}}, "spec": _pod()["spec"]},
        },
        "status": {
            "replicas": 2,
            "availableReplicas": 1,
            "updatedReplicas": 2,
            "observedGeneration": 3,
            "conditions": [
                {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
            ],
        },
    }
    rs = {
        "metadata": {
            "name": "agent-7c9f",
            "namespace": "dev",
            "annotations": {"deployment.kubernetes.io/revision": "2", "other": "x"},
            "ownerReferences": [{"kind": "Deployment", "name": "agent", "uid": "d-1"}],
        },
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "agent"}}},
        "status": {"replicas": 2, "readyReplicas": 1, "availableReplicas": 1},
    }

    snapshot = collect(
        FakeReader(
            {
                "Service": [service],
                "EndpointSlice": [slice_],
                "Deployment": [deployment],
                "ReplicaSet": [rs],
            }
        ),
        now=NOW,
    )

    svc = snapshot.items("Service")[0]
    assert svc["spec"] == {
        "type": "ClusterIP",
        "clusterIP": "10.96.0.10",
        "selector": {"app": "agent"},
        "ports": [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}],
    }
    es = snapshot.items("EndpointSlice")[0]
    assert es["metadata"]["labels"] == {"kubernetes.io/service-name": "agent"}
    assert es["endpoints"][0]["conditions"]["ready"] is False
    assert es["endpoints"][0]["targetRef"]["name"] == "agent-7c9f-x2k"
    assert "zone" not in es["endpoints"][0]
    dep = snapshot.items("Deployment")[0]
    assert dep["spec"]["replicas"] == 2
    assert dep["spec"]["progressDeadlineSeconds"] == 600
    assert "revisionHistoryLimit" not in dep["spec"]
    assert dep["spec"]["template"]["metadata"] == {"labels": {"app": "agent"}}
    assert dep["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] == "***"
    assert dep["status"]["conditions"][0]["reason"] == "ProgressDeadlineExceeded"
    rs_n = snapshot.items("ReplicaSet")[0]
    assert rs_n["metadata"]["annotations"] == {"deployment.kubernetes.io/revision": "2"}
    assert rs_n["metadata"]["ownerReferences"][0]["name"] == "agent"
    node = snapshot.items("Node")[0]
    assert node["spec"] == {"unschedulable": True}
    assert node["status"] == {
        "conditions": [{"type": "Ready", "status": "True", "reason": "KubeletReady"}]
    }


def test_fetch_logs_returns_redacted_tail_lines():
    reader = FakeReader(
        logs={("agent-7c9f-x2k", "agent", True): "start\npassword=hunter2\nERROR boom\n"}
    )

    lines = fetch_logs(reader, "agent-7c9f-x2k", "agent", previous=True, tail=50)

    assert lines == ["start", "password=***", "ERROR boom"]
    assert reader.log_calls == [("agent-7c9f-x2k", "agent", True, 50)]


def test_fetch_logs_masks_multiline_private_key():
    text = (
        "boot\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nzzQ9\n"
        "-----END RSA PRIVATE KEY-----\nok\n"
    )
    reader = FakeReader(logs={("p", "c", False): text})

    lines = fetch_logs(reader, "p", "c")

    assert "MIIEpAIBAAKCAQEA" not in str(lines)
    assert "zzQ9" not in str(lines)
    assert lines[0] == "boot" and lines[-1] == "ok"


def test_probe_header_values_are_masked():
    pod = _pod()
    pod["spec"]["containers"][0]["livenessProbe"] = {
        "httpGet": {
            "path": "/health",
            "port": 8080,
            "httpHeaders": [{"name": "Authorization", "value": "Basic dXNlcjpwYXNz"}],
        },
        "periodSeconds": 10,
    }

    container = collect(FakeReader({"Pod": [pod]}), now=NOW).items("Pod")[0]["spec"]["containers"][
        0
    ]

    probe = container["livenessProbe"]
    assert probe["httpGet"]["httpHeaders"] == [{"name": "Authorization", "value": "***"}]
    assert probe["httpGet"]["path"] == "/health"
    assert probe["periodSeconds"] == 10


def test_fetch_logs_grep_runs_on_redacted_lines():
    reader = FakeReader(logs={("p", "c", False): "ERROR a\ninfo\npassword=hunter2 ERROR\n"})

    assert fetch_logs(reader, "p", "c", grep="ERROR") == ["ERROR a", "password=*** ERROR"]
    # 비밀값으로 grep해도 줄이 나오지 않는다 (값을 추측하는 통로를 막음)
    assert fetch_logs(reader, "p", "c", grep="hunter2") == []


@pytest.mark.parametrize("tail", [0, 201])
def test_fetch_logs_rejects_tail_out_of_range(tail):
    with pytest.raises(ValueError, match="tail"):
        fetch_logs(FakeReader(), "p", "c", tail=tail)


def test_fetch_logs_failure_is_collect_failed():
    reader = FakeReader(logs={("p", "c", True): K8sCallError("400 Bad Request")})

    with pytest.raises(ServiceException) as exc:
        fetch_logs(reader, "p", "c", previous=True)

    assert exc.value.error_code is ErrorCode.COLLECT_FAILED


def test_resource_evidence_has_subject_and_api_source():
    snapshot = collect(FakeReader(), now=NOW)
    pod = snapshot.items("Pod")[0]

    evidence = resource_evidence(snapshot, pod)

    assert evidence.kind == "resource"
    assert evidence.subject == "dev/Pod/agent-7c9f-x2k"
    assert evidence.source == "GET /api/v1/namespaces/dev/pods/agent-7c9f-x2k"
    assert evidence.collected_at == NOW
    assert evidence.data == pod


def test_event_and_node_evidence_sources():
    event = {"metadata": {"name": "agent.1", "namespace": "dev"}, "message": "x"}
    snapshot = collect(FakeReader({"Event": [event]}), now=NOW)

    ev = resource_evidence(snapshot, snapshot.items("Event")[0])
    node = resource_evidence(snapshot, snapshot.items("Node")[0])

    assert ev.kind == "event"
    assert ev.source == "GET /api/v1/namespaces/dev/events/agent.1"
    assert node.subject == "Node/minikube"
    assert node.source == "GET /api/v1/nodes/minikube"


def test_log_evidence_records_container_and_previous_flag():
    evidence = log_evidence("dev", "agent-7c9f-x2k", "agent", ["a", "b"], NOW, previous=True)

    assert evidence.kind == "log_excerpt"
    assert evidence.subject == "dev/Pod/agent-7c9f-x2k"
    assert evidence.source == (
        "GET /api/v1/namespaces/dev/pods/agent-7c9f-x2k/log?container=agent&previous=true"
    )
    assert evidence.data == {"container": "agent", "previous": True, "lines": ["a", "b"]}


def test_collect_can_limit_kinds():
    reader = FakeReader({"Secret": K8sCallError("읽으면 안 됨")})

    snapshot = collect(reader, now=NOW, kinds=("Pod", "Service"))

    assert set(snapshot.resources) == {"Pod", "Service"}
    assert snapshot.failed == {}
    assert snapshot.items("Secret") is None


def test_collect_node_without_pod_kind_is_unknown():
    snapshot = collect(FakeReader(), now=NOW, kinds=("Node",))

    assert snapshot.items("Node") is None
    assert "Node" in snapshot.failed


def test_init_container_keeps_restart_policy_for_sidecar_detection():
    from service.sensors.collector import _pod_spec

    spec = _pod_spec(
        {"initContainers": [{"name": "side", "restartPolicy": "Always", "command": ["x"]}]}
    )

    assert spec["initContainers"] == [{"name": "side", "restartPolicy": "Always"}]
