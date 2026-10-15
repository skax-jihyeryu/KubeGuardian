"""도구 테스트용 가짜 객체: Kubernetes·HTTP 클라이언트와 메모리 근거 저장소 (네트워크 없음)."""

from datetime import UTC, datetime

from service.model import Evidence

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


class MemoryEvidence:
    def __init__(self):
        self.saved: list[Evidence] = []

    def add(self, evidence: Evidence) -> str:
        self.saved.append(evidence)
        return f"ev-{len(self.saved):04d}"


def pod(name, app="gateway", ip="10.0.0.5", restarts=0, port=8080, phase="Running", rs=None):
    return {
        "metadata": {
            "name": name,
            "namespace": "dev",
            "labels": {"app": app},
            "ownerReferences": [{"kind": "ReplicaSet", "name": rs or f"{app}-rs", "uid": "u"}],
        },
        "spec": {
            "nodeName": "minikube",
            "containers": [
                {
                    "name": app,
                    "image": f"{app}:1.0",
                    "ports": [{"name": "http", "containerPort": port}],
                    "env": [
                        {"name": "AGENT_SERVICE_URL", "value": "http://agent:8080"},
                        {"name": "DB_PASSWORD", "value": "hunter2"},
                        {
                            "name": "API_KEY",
                            "valueFrom": {"secretKeyRef": {"name": "gw-secret", "key": "k"}},
                        },
                    ],
                }
            ],
        },
        "status": {
            "phase": phase,
            "podIP": ip,
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": app,
                    "ready": True,
                    "restartCount": restarts,
                    "state": {"running": {"startedAt": "2026-10-20T00:01:00Z"}},
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
                    if restarts
                    else {},
                }
            ],
        },
    }


def service(name, selector=None, port=80, target_port="http"):
    return {
        "metadata": {"name": name, "namespace": "dev"},
        "spec": {
            "type": "ClusterIP",
            "selector": selector if selector is not None else {"app": name},
            "ports": [{"name": "http", "port": port, "targetPort": target_port}],
        },
    }


def deployment(name, env=None):
    return {
        "metadata": {"name": name, "namespace": "dev"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {"name": name, "image": f"{name}:1.0", "env": env or []},
                    ]
                },
            },
        },
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def replicaset(name, deployment_name):
    return {
        "metadata": {
            "name": name,
            "namespace": "dev",
            "ownerReferences": [{"kind": "Deployment", "name": deployment_name, "uid": "d"}],
        },
        "spec": {"replicas": 1},
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def event(reason, obj_kind, obj_name, last, type_="Warning", message="m", count=1):
    return {
        "metadata": {"name": f"{obj_name}.{reason}", "namespace": "dev"},
        "reason": reason,
        "message": message,
        "type": type_,
        "count": count,
        "lastTimestamp": last,
        "involvedObject": {"kind": obj_kind, "name": obj_name, "namespace": "dev"},
    }


class FakeK8s:
    """core.k8s.K8sClient를 흉내 낸다. kind → 객체 목록 또는 예외."""

    namespace = "dev"

    def __init__(self, objects=None, nodes=None, logs=None):
        self.objects = objects or {}
        self.nodes = nodes or []
        self.logs = logs or {}
        self.calls: list[str] = []
        self.log_calls: list[tuple] = []

    def list(self, kind):
        self.calls.append(kind)
        value = self.objects.get(kind, [])
        if isinstance(value, Exception):
            raise value
        return value

    def list_nodes(self):
        self.calls.append("Node")
        return self.nodes

    def read_log(self, pod, container, *, previous, tail_lines):
        self.log_calls.append((pod, container, previous, tail_lines))
        value = self.logs[(pod, container, previous)]
        if isinstance(value, Exception):
            raise value
        return value
