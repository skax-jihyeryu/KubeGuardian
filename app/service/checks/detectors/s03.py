"""S03 targetPort 불일치 (05-rules §4). 선언 비교라 즉시 판정한다.

- 이름으로 지정한 targetPort는 컨테이너 포트 이름으로 찾는다.
- 숫자 targetPort인데 대상 컨테이너가 ports를 하나도 선언하지 않았으면 "확인하지 못함"
  (컨테이너 포트 선언은 선택 사항이라 오탐이 나기 쉬움).
"""

from typing import Any

from service.checks.detectors._common import (
    inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    selects,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal, Verdict

RULE = "S03"
KINDS = ("Service", "Pod")


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    signals = []
    for svc in data["Service"]:
        name = name_of(svc)
        subj = subject(ctx, "Service", name)
        selector = svc["spec"].get("selector")
        targets = [p for p in data["Pod"] if selects(selector, p["metadata"].get("labels"))]
        if not selector or not targets:
            reason = "selector 없음" if not selector else "대상 Pod 0개 (S01)"
            signals.append(make_signal(RULE, "passed", subj, name, {"excluded": reason}))
            continue
        container_ports = _container_ports(targets)
        ports = []
        for p in svc["spec"].get("ports") or []:
            target = p.get("targetPort", p.get("port"))
            ports.append(
                {
                    "port": p.get("port"),
                    "targetPort": target,
                    "matched": _match(target, container_ports),
                }
            )
        values = {"ports": ports, "container_ports": container_ports}
        verdict: Verdict
        if any(p["matched"] is False for p in ports):
            verdict = "detected"
        elif any(p["matched"] is None for p in ports):
            verdict = "unknown"
        else:
            verdict = "passed"
        # 모든 포트가 어긋나면 요청을 하나도 받을 수 없다 → §3 기준, 일부면 주의
        all_broken = all(p["matched"] is False for p in ports)
        severity = primary_severity(ctx, name, fully_down=all_broken)
        signals.append(make_signal(RULE, verdict, subj, name, values, severity))
    return signals


def _container_ports(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pod in pods:
        for c in (pod.get("spec") or {}).get("containers") or []:
            for port in c.get("ports") or []:
                item = {k: port[k] for k in ("name", "containerPort") if k in port}
                if item not in out:
                    out.append(item)
    return out


def _match(target: Any, container_ports: list[dict[str, Any]]) -> bool | None:
    """True 일치 / False 불일치 / None 판정 불가 (숫자 targetPort, 선언된 컨테이너 포트 없음)."""
    if isinstance(target, str) and not target.isdigit():
        return any(p.get("name") == target for p in container_ports)
    if not container_ports:
        return None
    return any(p.get("containerPort") == int(target) for p in container_ports)
