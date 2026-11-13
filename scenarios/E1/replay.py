"""Replay synthetic login-failure observations through InvestigatorV1 without cluster or app I/O."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from core.config import KubeGuardianConfig
from core.db.observations import SqliteObservationStore
from core.db.tables import Base
from core.guard.budget import BudgetTracker
from core.http import HttpCallError, HttpResponse
from core.k8s import K8sCallError
from core.llm import build_chat_model
from service.agent.base import RunContext
from service.agent.investigator.v1.agent import InvestigatorV1
from service.checks.detectors.base import DetectorContext
from service.checks.detectors.p06 import detect
from service.checks.gate import decide_gate
from service.checks.signal_board import build_signal_board
from service.checks.symptoms import detect_symptoms
from service.model import InvestigationRequest
from service.sensors.collector import collect
from service.tools import build_tools
from service.tools.base import ToolContext

NOW = datetime(2026, 1, 1, 14, 4, tzinfo=UTC)
DECOY = "thumbnailer-v1-pod"
CONFIG = KubeGuardianConfig(
    target={"namespace": "replay"},
    core_paths={"signin": ["entry", "identity"]},
    api_scenarios=[
        {
            "name": "signin",
            "feature": "signin",
            "steps": [{"login": {"via": "entry", "path": "/signin", "account_secret": "replay"}}],
        }
    ],
)


class Evidence:
    def __init__(self):
        self.saved = {}

    def add(self, evidence):
        evidence_id = f"ev-{len(self.saved) + 1:04}"
        self.saved[evidence_id] = evidence
        return evidence_id


class Replay:
    namespace = "replay"

    def __init__(self, decoy):
        self.data = json.loads(Path(__file__).with_name("synthetic_observations.json").read_text())
        if decoy:
            self.pod_status(DECOY).update(
                restartCount=3,
                lastState={
                    "terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "finishedAt": "2026-01-01T09:00:01Z",
                    }
                },
            )

    def pod_status(self, name):
        return next(
            pod["status"]["containerStatuses"][0]
            for pod in self.data["resources"]["Pod"]
            if pod["metadata"]["name"] == name
        )

    def list(self, kind):
        if kind not in self.data["resources"]:
            raise K8sCallError(f"{kind} snapshot unavailable in replay")
        return self.data["resources"][kind]

    def list_nodes(self):
        raise K8sCallError("Node snapshot unavailable in replay")

    def read_log(
        self, pod, container, *, previous, tail_lines, since_seconds=None, timestamps=False
    ):
        records = self.data["log_observations"].get(pod)
        if previous or records is None or self.pod_status(pod)["name"] != container:
            raise K8sCallError("Requested log observation unavailable in replay")
        cutoff = NOW - timedelta(seconds=since_seconds) if since_seconds else None
        lines = []
        for record in records:
            timestamp = datetime.fromisoformat(record["timestamp"]).replace(tzinfo=UTC)
            if cutoff is None or timestamp >= cutoff:
                prefix = f"{timestamp.isoformat()} " if timestamps else ""
                lines.append(prefix + json.dumps(record, ensure_ascii=False))
        return "\n".join(lines[-tail_lines:])

    def account(self, secret_name):
        if secret_name != "replay":
            raise HttpCallError("Requested account unavailable in replay")
        return {}

    def post(self, *, service, port, path, timeout_s, json):
        step = self.data["login"]
        if (service, port, path) != (step["via"], step["port"], step["path"]):
            raise HttpCallError("Requested POST observation unavailable in replay")
        return HttpResponse(step["status"], step["latency_ms"], "")

    def get(self, **kwargs):
        raise HttpCallError("Requested GET observation unavailable in replay")

    def signals(self):
        current = collect(self, NOW, ("Pod", "ReplicaSet", "Service"))
        previous = current.model_copy(update={"collected_at": NOW - timedelta(minutes=1)})
        engine = create_engine("sqlite://")
        try:
            Base.metadata.create_all(engine)
            return detect(
                DetectorContext(
                    snapshot=current,
                    previous=previous,
                    now=NOW,
                    observations=SqliteObservationStore(engine),
                    config=CONFIG,
                )
            )
        finally:
            engine.dispose()

    def p06(self, name):
        return next(signal for signal in self.signals() if signal.values["pod"] == name)


def score(report):
    top = report.top_issue if report else None
    resource = top.cause_resource if top else None
    cause_names = {
        "Deployment": {"identity", "sessionstore"},
        "Service": {"identity", "sessionstore"},
        "Pod": {"identity-v1-pod", "sessionstore-v1-pod"},
    }
    expected = bool(
        top and top.rank == 1 and resource and resource.name in cause_names.get(resource.kind, ())
    )
    primary_claims = [report.conclusion, top.title if top else ""] if report else []
    mentions = [claim for claim in primary_claims if re.search(r"\bthumbnailer\b", claim, re.I)]
    mechanism = any(
        re.search(r"sessionstore|세션", claim, re.I)
        and re.search(r"timeout|timed out|시간 초과|타임아웃", claim, re.I)
        for claim in primary_claims
    )
    decoy_resource = bool(
        top
        and top.rank == 1
        and resource
        and resource.name in (DECOY, "thumbnailer", "thumbnailer-v1")
    )
    # Resource attribution is structured. Text mentions require reading the claim in context.
    return {
        "top1_expected": expected,
        "session_timeout_mentioned": mechanism,
        "decoy_rank1_resource": decoy_resource,
        "decoy_claims_for_review": mentions,
    }


def run(decoy, llm_factory):
    replay = Replay(decoy)
    signals = replay.signals()
    p06 = next(signal for signal in signals if signal.values["pod"] == DECOY)
    evidence = Evidence()
    budget = BudgetTracker(InvestigatorV1.spec.budget)
    ctx = RunContext(
        run_id="E1",
        namespace=replay.namespace,
        evidence=evidence,
        budget=budget,
        k8s=replay,
        http=replay,
        now=lambda: NOW,
        scenario_services={"signin": ["entry", "identity"]},
    )
    tools = build_tools(CONFIG)
    symptoms = detect_symptoms(
        CONFIG,
        ToolContext(
            namespace=replay.namespace, evidence=evidence, k8s=replay, http=replay, now=lambda: NOW
        ),
    )
    gate = decide_gate(signals, symptoms)
    request = InvestigationRequest(
        trigger="check",
        gate_path=gate.path,
        signal_board=build_signal_board(signals),
        symptoms=gate.symptoms,
    )
    # The production environment notes can contain deployment-specific data.
    with patch(
        "service.agent.investigator.v1.graph.KNOWLEDGE_DIR",
        Path(__file__).with_name("knowledge"),
    ):
        result = InvestigatorV1(llm_factory, tools, ctx).run(request)
    return {
        "variant": "decoy" if decoy else "clean",
        "gate": gate.path,
        "p06": p06.model_dump(mode="json"),
        "score": score(result.report),
        "decoy_observed": any(
            ev.data.get("kind") == "Pod" and any(item["name"] == DECOY for item in ev.data["items"])
            for ev in evidence.saved.values()
        ),
        "evidence": {
            evidence_id: item.model_dump(mode="json")
            for evidence_id, item in evidence.saved.items()
        },
        "result": result.model_dump(mode="json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=("clean", "decoy"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = run(args.variant == "decoy", build_chat_model)
    output["model_mode"] = "live"
    output["models"] = InvestigatorV1.spec.models
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return int(output["result"]["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
