"""시나리오 채점: 규칙 채점(`score`)과 AI 조사 결과의 네 층 채점(결정적 대조·실패 분류).

정답 파일의 신호 구분 (05-rules §0.1.1):
- must_signals: 반드시 적발되어야 하는 장애 신호
- allowed_signals: 나와도 되는 장애 신호 (예: A3의 P03)
- forbidden_signals: 나오면 오탐인 장애 신호. `"*"`는 must·allowed를 뺀 장애 신호 전부
  (개선 권고 H는 포함하지 않는다)

통과 = must를 모두 적발 + 게이트 경로가 기대와 같음 + forbidden을 하나도 적발하지 않음.
그 밖의 적발(unexpected)은 오탐 후보로 기록만 한다.
must_check와 서술형 not_a_cause는 채점하지 않고 기록만 한다. 보고서와의 대조는
`coverage_record`가 영역별 확인 여부로 기록하고, 통과 조건에는 W4 평가부터 넣는다 (spec D2).
`not_a_cause`의 resource가 있는 항목은 금지 층으로 `cause_record`가 원인 리소스와 대조한다.

AI 조사 결과는 네 층으로 채점한다: 결정적 대조(원인 리소스 `cause_record`, 핵심 근거
`key_evidence_record`), 판정 일치(judge), 결론-근거 지지(conclusion_support). `classify`가
이 결과를 실패 분류로 합친다: pass, F1(도구 한계), F2(핵심 근거 누락), F3(원인 오지목),
F4(원인은 맞았으나 판정 불일치), F5(결론이 근거로 지지되지 않음). F0·error는 호출한 쪽이 정한다.
"""

import copy
import fnmatch
import json
from typing import Any, Literal

from service.checks.detectors.catalog import CATALOG
from service.model import GatePath, Signal


def _rules(signals: list[Signal], verdict: str, grade: str) -> set[str]:
    return {
        s.rule_id for s in signals if s.verdict == verdict and CATALOG[s.rule_id].grade == grade
    }


def expected_record(expected: dict[str, Any]) -> dict[str, Any]:
    """채점하지 않고 result.json에 그대로 남기는 정답 항목."""
    return {
        "must_check": expected.get("must_check") or {},
        "not_a_cause": expected.get("not_a_cause") or [],
    }


def coverage_record(must_check: dict[str, str], coverage: dict[str, Any]) -> dict[str, dict]:
    """must_check 영역마다 보고서 coverage의 상태와 근거 유무를 기록한다. 채점하지 않는다 (D2)."""
    record = {}
    for area, expected in must_check.items():
        cov = coverage.get(area) or {}
        record[area] = {
            "expected": expected,
            "status": cov.get("status", "not_checked"),
            "has_evidence": bool(cov.get("evidence_ids")),
        }
    return record


def score(expected: dict[str, Any], signals: list[Signal], gate_path: GatePath) -> dict[str, Any]:
    want = set(expected.get("must_signals") or [])
    detected = _rules(signals, "detected", "signal")
    allowed = set(expected.get("allowed_signals") or [])
    forbidden = set(expected.get("forbidden_signals") or [])
    forbidden_hit = detected - want - allowed if "*" in forbidden else detected & forbidden
    gate_ok = gate_path == expected["gate_path"]
    missed = want - detected
    return {
        "passed": not missed and gate_ok and not forbidden_hit,
        "gate_path": gate_path,
        "gate_expected": expected["gate_path"],
        "gate_ok": gate_ok,
        "expected": sorted(want),
        "detected": sorted(detected),
        "missed": sorted(missed),
        "forbidden_hit": sorted(forbidden_hit),
        "unexpected": sorted(detected - want - allowed - forbidden_hit),
        "observing": sorted(_rules(signals, "observing", "signal")),
        "unknown": sorted({s.rule_id for s in signals if s.verdict == "unknown"}),
        "advice": sorted(_rules(signals, "detected", "advice")),
    }


PLACEHOLDER = "${injected_target}"

Failure = Literal["pass", "F1", "F2", "F3", "F4", "F5"]


def _key_patterns(expected: dict[str, Any]):
    for key in expected.get("key_evidence") or []:
        yield from key.get("any_of") or []


def needs_target(expected: dict[str, Any]) -> bool:
    """정답에 주입 대상 자리표시자가 있는지."""
    if any(PLACEHOLDER in (c.get("name") or "") for c in expected.get("expected_cause") or []):
        return True
    return any(
        PLACEHOLDER in json.dumps([p.get("subject"), p.get("contains")], ensure_ascii=False)
        for p in _key_patterns(expected)
    )


def resolve_expected(expected: dict[str, Any], target: str | None) -> dict[str, Any]:
    """자리표시자를 주입 대상 이름으로 바꾼 복사본. 자리표시자가 있던 원인 항목은 exact로 둔다."""
    if needs_target(expected) and not target:
        raise ValueError("주입 대상을 읽지 못함")
    out = copy.deepcopy(expected)
    for cause in out.get("expected_cause") or []:
        if PLACEHOLDER in (cause.get("name") or ""):
            cause["name"] = cause["name"].replace(PLACEHOLDER, target)
            cause["exact"] = True
    for p in _key_patterns(out):
        if PLACEHOLDER in (p.get("subject") or ""):
            p["subject"] = p["subject"].replace(PLACEHOLDER, target)
        contains = p.get("contains")
        if isinstance(contains, str):
            p["contains"] = contains.replace(PLACEHOLDER, target)
        elif isinstance(contains, list):
            p["contains"] = [c.replace(PLACEHOLDER, target) for c in contains]
    return out


def resource_matches(ref: dict[str, Any] | None, want: dict[str, Any]) -> bool:
    """kind 대소문자 무시, name 정확히 일치. exact 아닌 Deployment 정답은 하위 Pod·RS도 인정."""
    if not ref:
        return False
    kind = (ref.get("kind") or "").lower()
    name = ref.get("name") or ""
    if kind == want["kind"].lower() and name == want["name"]:
        return True
    return (
        not want.get("exact")
        and want["kind"].lower() == "deployment"
        and kind in ("pod", "replicaset")
        and name.startswith(want["name"] + "-")
    )


def cause_record(cause: dict[str, Any] | None, expected: dict[str, Any]) -> dict[str, bool]:
    """지목한 원인 리소스를 expected_cause와 not_a_cause(resource가 있는 항목)에 대조한다."""
    forbidden = [
        item["resource"]
        for item in expected.get("not_a_cause") or []
        if isinstance(item, dict) and item.get("resource")
    ]
    return {
        "cause_match": any(
            resource_matches(cause, w) for w in expected.get("expected_cause") or []
        ),
        "forbidden_cause": any(resource_matches(cause, w) for w in forbidden),
    }


def _evidence_matches(pattern: dict[str, Any], ev: dict[str, Any]) -> bool:
    if ev.get("kind") != pattern.get("kind"):
        return False
    if not fnmatch.fnmatchcase(ev.get("subject") or "", pattern.get("subject") or "*"):
        return False
    contains = pattern.get("contains")
    if not contains:
        return True
    wanted = [contains] if isinstance(contains, str) else contains
    text = json.dumps(ev.get("data"), ensure_ascii=False).lower()
    return all(w.lower() in text for w in wanted)


def key_evidence_record(
    key_evidence: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    """핵심 근거마다 수집된 evidence 중 하나라도 맞는지. 얻을 수 없는 근거는 누락이 아니다."""
    found, missing = [], []
    for key in key_evidence:
        if any(_evidence_matches(p, ev) for p in key.get("any_of") or [] for ev in evidence):
            found.append(key["id"])
        elif key.get("obtainable"):
            missing.append(key["id"])
    return {
        "key_found": found,
        "key_missing": missing,
        "key_all_found": not missing,
        "tool_limited": bool(key_evidence) and not any(k.get("obtainable") for k in key_evidence),
    }


def classify(
    *,
    cause_match: bool,
    judge_match: bool,
    forbidden_cause: bool,
    key: dict[str, Any],
    conclusion_support: str | None,
) -> Failure:
    """실패 분류 (spec §4). F0과 error는 호출한 쪽이 정한다."""
    if cause_match and judge_match:
        return "F5" if conclusion_support == "unsupported" else "pass"
    if key["tool_limited"]:
        return "F1"
    if key["key_missing"]:
        return "F2"
    if not cause_match or forbidden_cause:
        return "F3"
    return "F4"
