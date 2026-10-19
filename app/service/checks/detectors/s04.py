"""S04 서비스 주소 설정 오류 (05-rules §4). 토폴로지의 unresolved 엣지를 서비스별로 판정한다.

- 같은 서비스에 unresolved 주소가 하나라도 있으면 적발이 우선한다.
- 그 외에 envFrom ConfigMap·Secret을 수집하지 못한 서비스(`Topology.unchecked`, #37)는
  확인하지 못함. 아직 그 필드가 없는 토폴로지에서도 동작한다.
- `external_hosts` 허용 목록의 호스트는 토폴로지가 이미 뺀다.
"""

from typing import Any
from urllib.parse import urlsplit

from service.checks.detectors._common import (
    inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal, TopologyEdge, Verdict
from service.sensors.topology import build_topology

RULE = "S04"
KINDS = ("Service", "Deployment")


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    topology = build_topology(ctx.snapshot, ctx.config) if data is not None else None
    if data is None or topology is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    unchecked: dict[str, list[str]] = getattr(topology, "unchecked", None) or {}
    env_values = _env_values(data["Deployment"])
    by_source: dict[str, list[TopologyEdge]] = {}
    for edge in topology.edges:
        by_source.setdefault(edge.source, []).append(edge)
    service_names = {name_of(s) for s in data["Service"]}
    signals = []
    for source in sorted(set(by_source) | set(unchecked)):
        edges = by_source.get(source, [])
        values: dict[str, Any] = {
            "service": source,
            "addresses": [_address(e, env_values) for e in edges],
        }
        if source in unchecked:
            values["unchecked"] = unchecked[source]
        verdict: Verdict
        if any(e.status == "unresolved" for e in edges):
            verdict = "detected"
        elif source in unchecked:
            verdict = "unknown"
        else:
            verdict = "passed"
        kind = "Service" if source in service_names else "Deployment"
        subj = subject(ctx, kind, source)
        # 그 env를 쓰는 서비스가 핵심 경로면 긴급, 아니면 주의
        severity = primary_severity(ctx, source, fully_down=True)
        signals.append(make_signal(RULE, verdict, subj, source, values, severity))
    return signals


def _env_values(deployments: list[dict[str, Any]]) -> dict[str, list[str]]:
    """env 이름 → 값 목록 (수집기가 마스킹한 값). 근거 표시용."""
    out: dict[str, list[str]] = {}
    for dep in deployments:
        spec = ((dep["spec"].get("template") or {}).get("spec")) or {}
        for c in spec.get("containers") or []:
            for env in c.get("env") or []:
                if env.get("value"):
                    out.setdefault(env.get("name", ""), []).append(env["value"])
    return out


def _address(edge: TopologyEdge, env_values: dict[str, list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {"env": edge.env_name}
    # 같은 이름의 env가 여럿이면 이 엣지의 호스트를 가리키는 값만 쓴다 (envFrom 값은 없을 수 있음)
    values = [v for v in env_values.get(edge.env_name or "", []) if _points_to(v, edge.target)]
    if len(values) == 1:
        out["value"] = values[0]
    return out | {"target": edge.target, "port": edge.port, "status": edge.status}


def _host(value: str) -> str | None:
    try:
        return urlsplit(value if "://" in value else f"//{value}").hostname
    except ValueError:
        return None


def _points_to(value: str, target: str) -> bool:
    """값의 호스트가 엣지 대상과 같은가 (declared 엣지의 대상은 `name.ns.svc`의 name)."""
    host = _host(value)
    return host is not None and target in (host, host.split(".")[0])
