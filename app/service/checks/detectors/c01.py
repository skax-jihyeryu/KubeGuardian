"""C01 설정 참조 누락 (05-rules §4). 선언 비교라 즉시 판정한다.

Pod spec의 envFrom, env[].valueFrom(configMapKeyRef·secretKeyRef), volumes(configMap·secret,
projected 포함)가 참조하는 객체나 키가 없으면 적발한다. `optional: true`는 제외한다.
Secret은 수집기가 키 이름만 남기므로 값은 보지 않는다.
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
from service.model import Signal

RULE = "C01"
KINDS = ("Pod", "ConfigMap", "Secret", "ReplicaSet", "Service")

Ref = dict[str, Any]  # where, kind, name, key?, optional


@detector(RULE)
def detect(ctx: DetectorContext) -> list[Signal]:
    data = inputs(ctx, *KINDS)
    if data is None:
        return [rule_unknown(ctx, RULE, KINDS)]
    keys = {
        "ConfigMap": {
            name_of(cm): set(cm.get("data") or {}) | set(cm.get("binaryDataKeys") or [])
            for cm in data["ConfigMap"]
        },
        "Secret": {name_of(s): set(s.get("keys") or []) for s in data["Secret"]},
    }
    wl = Workloads(data["Pod"], data["ReplicaSet"], data["Service"])
    signals = []
    for pod in data["Pod"]:
        refs = []
        for ref in _references(pod.get("spec") or {}):
            objects = keys[ref["kind"]]
            exists = ref["name"] in objects and (
                ref.get("key") is None or ref["key"] in objects[ref["name"]]
            )
            refs.append({**ref, "exists": exists})
        missing = [r for r in refs if not r["exists"] and not r["optional"]]
        values: dict[str, Any] = {"references": refs}
        if missing:
            values["missing"] = missing
        service = wl.service_of_pod(pod)
        # 참조하는 Pod는 시작하지 못한다. 같은 Deployment의 다른 Pod가 Ready면 주의
        severity = primary_severity(ctx, service, fully_down=wl.ready_pods_like(pod) == 0)
        verdict = "detected" if missing else "passed"
        subj = subject(ctx, "Pod", name_of(pod))
        signals.append(make_signal(RULE, verdict, subj, service, values, severity))
    return signals


def _references(spec: dict[str, Any]) -> list[Ref]:
    refs: list[Ref] = []
    for c in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
        where = f"container {c.get('name')}"
        for src in c.get("envFrom") or []:
            for field, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                if ref := src.get(field):
                    refs.append(_ref(f"{where} envFrom", kind, ref.get("name"), None, ref))
        for env in c.get("env") or []:
            value_from = env.get("valueFrom") or {}
            for field, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
                if ref := value_from.get(field):
                    where_env = f"{where} env {env.get('name')}"
                    refs.append(_ref(where_env, kind, ref.get("name"), ref.get("key"), ref))
    for vol in spec.get("volumes") or []:
        where = f"volume {vol.get('name')}"
        sources = [vol] + list((vol.get("projected") or {}).get("sources") or [])
        for src in sources:
            if cm := src.get("configMap"):
                refs.extend(_volume_refs(where, "ConfigMap", cm.get("name"), cm))
            if sec := src.get("secret"):
                refs.extend(
                    _volume_refs(where, "Secret", sec.get("secretName") or sec.get("name"), sec)
                )
    return refs


def _volume_refs(where: str, kind: str, name: str | None, src: dict[str, Any]) -> list[Ref]:
    items = src.get("items") or []
    if not items:
        return [_ref(where, kind, name, None, src)]
    return [_ref(where, kind, name, item.get("key"), src) for item in items]


def _ref(where: str, kind: str, name: str | None, key: str | None, src: dict[str, Any]) -> Ref:
    ref: Ref = {"where": where, "kind": kind, "name": name, "optional": bool(src.get("optional"))}
    if key is not None:
        ref["key"] = key
    return ref
