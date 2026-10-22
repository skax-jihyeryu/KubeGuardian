"""C05 필수 설정 누락 (05-rules §4). `required_config`로 선언한 env 이름의 부재만 적발한다.

선언하지 않은 부재는 판정하지 않는다 (§0.1.1 의도일 수 있음). 선언 비교라 즉시 판정한다.
- env(valueFrom·빈 값 포함)와 envFrom(ConfigMap 키, Secret은 키 이름만, prefix 적용)을 본다.
  컨테이너가 여럿이면 어느 하나에 있으면 충족이다.
- 선언한 서비스가 없으면 설정 오류로 적발한다 (오타가 조용히 통과하지 않게).
- 없는 ConfigMap·Secret을 참조하는 envFrom이 그 이름을 낼 수 있으면(prefix 일치) 확인하지 못함.
  없는 참조 자체는 C01이 적발한다.
- Service 수집에 실패하면 선언한 서비스가 있는지 알 수 없으므로 확인하지 못함.
"""

from typing import Any

from service.checks.detectors._common import (
    Workloads,
    inputs,
    make_signal,
    name_of,
    primary_severity,
    rule_unknown,
    subject,
)
from service.checks.detectors.base import DetectorContext, detector
from service.model import Signal, Verdict

RULE = "C05"
KINDS = ("Deployment", "ConfigMap", "Secret", "Service")
Obj = dict[str, Any]


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    required = ctx.config.required_config
    if not required:
        return []
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    wl = Workloads([], None, data["Service"])
    config_maps = {
        name_of(c): set(c.get("data") or {}) | set(c.get("binaryDataKeys") or [])
        for c in data["ConfigMap"]
    }
    secrets = {name_of(s): set(s.get("keys") or []) for s in data["Secret"]}
    by_service: dict[str, list[Obj]] = {}
    for dep in data["Deployment"]:
        by_service.setdefault(wl.service_of_deployment(dep), []).append(dep)
    service_names = {name_of(s) for s in data["Service"]}
    signals = []
    for service in sorted(required):
        kind = "Service" if service in service_names else "Deployment"
        subj = subject(ctx, kind, service)
        severity = primary_severity(ctx, service, fully_down=True)
        deployments = by_service.get(service)
        if not deployments:
            values: dict[str, Any] = {"service": service, "error": "선언한 서비스 없음"}
            signals.append(make_signal(RULE, "detected", subj, service, values, severity))
            continue
        containers = [
            c
            for dep in deployments
            for c in ((dep["spec"].get("template") or {}).get("spec") or {}).get("containers") or []
        ]
        found: dict[str, str] = {}
        missing, unchecked = [], []
        for name in required[service]:
            where = _find(name, containers, config_maps, secrets)
            if where is not None:
                found[name] = where
            elif _may_come_from_missing_ref(name, containers, config_maps, secrets):
                unchecked.append(name)
            else:
                missing.append(name)
        verdict: Verdict = "detected" if missing else "unknown" if unchecked else "passed"
        values = {
            "service": service,
            "required": list(required[service]),
            "found": found,
            "missing": missing,
            "unchecked": unchecked,
        }
        signals.append(make_signal(RULE, verdict, subj, service, values, severity))
    return signals


def _find(
    name: str,
    containers: list[Obj],
    config_maps: dict[str, set[str]],
    secrets: dict[str, set[str]],
) -> str | None:
    """이름이 있는 위치. env가 envFrom보다 먼저다. 없으면 None."""
    for c in containers:
        if any(e.get("name") == name for e in c.get("env") or []):
            kind = "valueFrom" if _is_value_from(name, c) else "env"
            return kind
    for c in containers:
        for src in c.get("envFrom") or []:
            prefix = src.get("prefix") or ""
            for field, objects, label in (
                ("configMapRef", config_maps, "configmap"),
                ("secretRef", secrets, "secret"),
            ):
                ref_name = (src.get(field) or {}).get("name")
                if ref_name in objects and name in {prefix + k for k in objects[ref_name]}:
                    return f"{label}:{ref_name}"
    return None


def _is_value_from(name: str, container: Obj) -> bool:
    return any(
        e.get("name") == name and "valueFrom" in e and "value" not in e
        for e in container.get("env") or []
    )


def _may_come_from_missing_ref(
    name: str,
    containers: list[Obj],
    config_maps: dict[str, set[str]],
    secrets: dict[str, set[str]],
) -> bool:
    """없는(optional 아닌) ConfigMap·Secret envFrom이 이 이름을 낼 수 있는가 (prefix 일치)."""
    for c in containers:
        for src in c.get("envFrom") or []:
            for field, objects in (("configMapRef", config_maps), ("secretRef", secrets)):
                ref = src.get(field)
                missing_ref = ref and ref.get("name") not in objects and not ref.get("optional")
                if missing_ref and name.startswith(src.get("prefix") or ""):
                    return True
    return False
