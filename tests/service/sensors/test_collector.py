from datetime import UTC, datetime

import pytest

from core.error.service_exception import ErrorCode, ServiceException
from core.k8s import LOG_LIMIT_BYTES, K8sCallError
from service.sensors.collector import (
    COLLECTED_KINDS,
    MAX_LOG_SCAN,
    collect,
    fetch_logs,
    log_evidence,
    parse_grep,
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
        self.log_since = []

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

    def read_log(self, pod, container, *, previous, tail_lines, since_seconds=None):
        self.log_calls.append((pod, container, previous, tail_lines))
        self.log_since.append(since_seconds)
        value = self.logs[(pod, container, previous)]
        if isinstance(value, Exception):
            raise value
        if callable(value):  # 줄 수에 따라 본문이 달라지는 테스트용
            return value(tail_lines)
        return "".join(value.splitlines(keepends=True)[-tail_lines:])  # 실제 API처럼 끝 tail줄만


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

    assert lines.lines == ["start", "password=***", "ERROR boom"]
    assert (lines.scanned, lines.more_before, lines.match_count) == (3, False, None)
    assert reader.log_calls == [("agent-7c9f-x2k", "agent", True, 51)]  # 앞부분 확인용으로 하나 더


def test_fetch_logs_masks_multiline_private_key():
    text = (
        "boot\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nzzQ9\n"
        "-----END RSA PRIVATE KEY-----\nok\n"
    )
    reader = FakeReader(logs={("p", "c", False): text})

    lines = fetch_logs(reader, "p", "c").lines

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

    assert fetch_logs(reader, "p", "c", grep="ERROR").lines == ["ERROR a", "password=*** ERROR"]
    # 비밀값으로 grep해도 줄이 나오지 않는다 (값을 추측하는 통로를 막음)
    assert fetch_logs(reader, "p", "c", grep="hunter2").lines == []


@pytest.mark.parametrize("tail", [0, 201])
def test_fetch_logs_rejects_tail_out_of_range(tail):
    with pytest.raises(ValueError, match="tail"):
        fetch_logs(FakeReader(), "p", "c", tail=tail)


def test_fetch_logs_failure_is_collect_failed():
    reader = FakeReader(logs={("p", "c", True): K8sCallError("400 Bad Request")})

    with pytest.raises(ServiceException) as exc:
        fetch_logs(reader, "p", "c", previous=True)

    assert exc.value.error_code is ErrorCode.COLLECT_FAILED


def _numbered(n, suffix=""):
    return "\n".join(f"line{i}{suffix}" for i in range(n))


def test_fetch_logs_more_before_only_when_lines_exceed_tail():
    reader = FakeReader(logs={("p", "c", False): _numbered(10)})

    exact = fetch_logs(reader, "p", "c", tail=10)
    more = fetch_logs(reader, "p", "c", tail=9)

    assert (exact.more_before, exact.scanned, len(exact.lines)) == (False, 10, 10)
    assert (more.more_before, more.scanned, len(more.lines)) == (True, 10, 9)
    assert more.lines[-1] == "line9"


def test_fetch_logs_grep_is_or_and_case_insensitive():
    text = "Quota exceeded\nHTTP Request ok\nplain\nTIMEOUT here"
    reader = FakeReader(logs={("p", "c", False): text})

    result = fetch_logs(reader, "p", "c", grep=" quota | timeout ")

    assert result.lines == ["Quota exceeded", "TIMEOUT here"]
    assert result.match_count == 2


def test_fetch_logs_grep_is_substring_not_regex():
    reader = FakeReader(logs={("p", "c", False): "429 Too Many\nabc"})

    assert fetch_logs(reader, "p", "c", grep="4.9").lines == []


def test_fetch_logs_grep_finds_lines_beyond_tail():
    text = "needle early\n" + _numbered(1500)
    reader = FakeReader(logs={("p", "c", False): text})

    result = fetch_logs(reader, "p", "c", tail=100, grep="needle")

    assert result.lines == ["needle early"]
    assert reader.log_calls == [("p", "c", False, MAX_LOG_SCAN)]
    assert (result.scanned, result.more_before, result.match_count) == (1501, False, 1)


def test_fetch_logs_grep_limits_to_last_tail_matches_and_counts_all():
    reader = FakeReader(logs={("p", "c", False): _numbered(30, " ERROR")})

    result = fetch_logs(reader, "p", "c", tail=10, grep="error")

    assert result.lines == [f"line{i} ERROR" for i in range(20, 30)]
    assert (result.match_count, result.more_before) == (30, True)


def test_fetch_logs_grep_more_before_when_scan_limit_reached():
    reader = FakeReader(logs={("p", "c", False): _numbered(MAX_LOG_SCAN)})

    result = fetch_logs(reader, "p", "c", grep="line5")

    assert result.more_before is True and result.scanned == MAX_LOG_SCAN


def test_fetch_logs_grep_no_more_before_when_all_read_and_few_matches():
    reader = FakeReader(logs={("p", "c", False): _numbered(20)})

    result = fetch_logs(reader, "p", "c", tail=5, grep="line1")

    assert result.match_count == 11 and result.more_before is True  # 11 > tail
    result = fetch_logs(reader, "p", "c", tail=20, grep="line1")
    assert result.match_count == 11 and result.more_before is False


def test_fetch_logs_passes_since_seconds():
    reader = FakeReader(logs={("p", "c", False): "a"})

    fetch_logs(reader, "p", "c", since_seconds=1800)
    fetch_logs(reader, "p", "c")

    assert reader.log_since == [1800, None]


@pytest.mark.parametrize("grep", ["|||", " | ", "|"])
def test_fetch_logs_rejects_grep_without_keyword(grep):
    with pytest.raises(ValueError, match="키워드"):
        fetch_logs(FakeReader(), "p", "c", grep=grep)


def test_fetch_logs_retries_with_quarter_lines_when_byte_limit_reached():
    big = "x" * LOG_LIMIT_BYTES

    def body(lines):
        return big if lines > 100 else _numbered(lines)

    reader = FakeReader(logs={("p", "c", False): body})

    result = fetch_logs(reader, "p", "c", tail=100)

    assert [c[3] for c in reader.log_calls] == [101, 25]
    assert result.more_before is True
    assert result.lines[-1] == "line24"


def test_fetch_logs_fails_after_three_retries_at_byte_limit():
    big = "x" * LOG_LIMIT_BYTES
    reader = FakeReader(logs={("p", "c", False): lambda lines: big})

    with pytest.raises(ServiceException) as exc:
        fetch_logs(reader, "p", "c", grep="x")

    assert exc.value.error_code is ErrorCode.COLLECT_FAILED
    assert [c[3] for c in reader.log_calls] == [2000, 500, 125, 31]


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


def test_replica_set_keeps_template_image_and_env_names():
    rs = {
        "metadata": {"name": "agent-7c9f", "namespace": "dev"},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"app": "agent"}},
                "spec": {
                    "containers": [
                        {
                            "name": "agent",
                            "image": "agent:v1",
                            "env": [
                                {"name": "DB_PASSWORD", "value": "hunter2"},
                                {
                                    "name": "X",
                                    "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}},
                                },
                            ],
                        }
                    ]
                },
            },
        },
    }

    rs_n = collect(FakeReader({"ReplicaSet": [rs]}), now=NOW).items("ReplicaSet")[0]

    c = rs_n["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "agent:v1"
    assert [e["name"] for e in c["env"]] == ["DB_PASSWORD", "X"]
    assert c["env"][0]["value"] == "***"


# ── 숫자 키워드 경계 ──


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-20T00:01:03.502Z INFO ok",  # 타임스탬프의 소수부
        "2026-10-20T00:01:03,502 INFO ok",
        "request id=7f4290 done",  # 더 긴 숫자의 일부
        "request id=7f502 done",  # 16진 id의 일부
        "took 1502ms",
        "at 12:502 sec",
    ],
)
def test_fetch_logs_numeric_grep_ignores_digits_inside_other_numbers(line):
    reader = FakeReader(logs={("p", "c", False): line})

    assert fetch_logs(reader, "p", "c", grep="502|429").lines == []


@pytest.mark.parametrize(
    "line",
    ["upstream returned HTTP 502", "status=429 retry", '{"code":429,"msg":"x"}', "502", "(502)"],
)
def test_fetch_logs_numeric_grep_matches_status_codes(line):
    reader = FakeReader(logs={("p", "c", False): line})

    assert fetch_logs(reader, "p", "c", grep="502|429").lines == [line]


def test_fetch_logs_mixed_keywords_still_use_substring_for_words():
    reader = FakeReader(logs={("p", "c", False): "Timeout after 5s\n03.502Z\nHTTP 502"})

    result = fetch_logs(reader, "p", "c", grep="timeout|502")

    assert result.lines == ["Timeout after 5s", "HTTP 502"]


def test_parse_grep_splits_on_escaped_pipe_too():
    assert parse_grep(r"error\|timeout | refused") == ["error", "timeout", "refused"]


# ── 마스킹 전 원문 줄 수 기준 ──

_PEM_BODY = "\n".join(
    ["-----BEGIN RSA PRIVATE KEY-----", "A" * 20, "B" * 20, "-----END RSA PRIVATE KEY-----"]
)


def test_fetch_logs_more_before_counts_raw_lines_when_pem_collapses():
    text = "\n".join(["l0", _PEM_BODY, "l5"])  # 원문 6줄, 마스킹하면 3줄
    reader = FakeReader(logs={("p", "c", False): text})

    result = fetch_logs(reader, "p", "c", tail=5)

    assert result.scanned == 6 and result.more_before is True
    assert "A" * 20 not in str(result.lines)


def test_fetch_logs_grep_scan_limit_counts_raw_lines_when_pem_collapses():
    body = ["l"] * (MAX_LOG_SCAN - 4) + _PEM_BODY.split("\n")  # 원문 2000줄
    reader = FakeReader(logs={("p", "c", False): "\n".join(body)})

    result = fetch_logs(reader, "p", "c", grep="zzz")

    assert result.scanned == MAX_LOG_SCAN and result.more_before is True


# ── 끝 개행과 다중 바이트 ──


def test_fetch_logs_exact_tail_lines_with_trailing_newline_is_not_truncated():
    reader = FakeReader(logs={("p", "c", False): _numbered(10) + "\n"})

    exact = fetch_logs(reader, "p", "c", tail=10)
    more = fetch_logs(reader, "p", "c", tail=9)

    assert (exact.more_before, len(exact.lines), exact.scanned) == (False, 10, 10)
    assert (more.more_before, len(more.lines), more.scanned) == (True, 9, 10)


def test_fetch_logs_detects_byte_limit_when_cut_inside_multibyte_char():
    cut = ("가" * LOG_LIMIT_BYTES).encode()[: LOG_LIMIT_BYTES - 1].decode("utf-8", "replace")
    assert len(cut) < LOG_LIMIT_BYTES  # 글자 수로는 한도 밑이다

    reader = FakeReader(logs={("p", "c", False): lambda n: cut if n > 100 else _numbered(n)})

    result = fetch_logs(reader, "p", "c", tail=100)

    assert [c[3] for c in reader.log_calls] == [101, 25]
    assert result.more_before is True


def test_fetch_logs_reports_requested_lines_after_retry():
    reader = FakeReader(
        logs={("p", "c", False): lambda n: "x" * LOG_LIMIT_BYTES if n > 100 else _numbered(n)}
    )

    assert fetch_logs(reader, "p", "c", tail=100).requested_lines == 25
    assert (
        fetch_logs(FakeReader(logs={("p", "c", False): "a"}), "p", "c", tail=100).requested_lines
        == 101
    )
