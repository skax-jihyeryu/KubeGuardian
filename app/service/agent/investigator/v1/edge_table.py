"""호출 구간 근거표: 경로 B 사전 확인에서 실패 경로의 호출 구간마다 관찰 사실을 한 줄로 모은다.

구간(topology edge)마다 호출하는 쪽 로그, 받는 쪽 Pod Ready 수, 받는 쪽 오류 로그를 본다.
분류는 관찰한 오류 기록의 유무만으로 정하고 원인을 단정하지 않는다.
"""

import re

ERROR_WORDS = re.compile(r"error|exception|fail|timeout|refused|429|502|503|504", re.IGNORECASE)

BOTH = "양쪽 오류"
CALLER_ONLY = "호출하는 쪽만 오류, 구간 불일치"
TARGET_ONLY = "받는 쪽만 오류"
NONE = "오류 기록 없음"
UNCHECKED = "확인하지 못함"


def select_edges(edges: list[dict], path: list[str], limit: int) -> list[dict]:
    """경로 서비스가 호출하는 구간을 경로 순서로 limit개 고른다.

    경로의 다음 서비스로 가는 구간을 먼저, 그다음 경로 밖(또는 건너뛴) 의존성이다.
    해석하지 못한 주소(unresolved)·사이드카·중복 구간은 뺀다.
    """
    seen: set[tuple[str, str]] = set()
    candidates = []
    for order, e in enumerate(edges):
        key = (e["source"], e["target"])
        if (
            e["source"] not in path
            or e.get("status") != "declared"
            or e.get("via") == "sidecar"
            or e["target"] == e["source"]
            or key in seen
        ):
            continue
        seen.add(key)
        index = path.index(e["source"])
        next_hop = index + 1 < len(path) and e["target"] == path[index + 1]
        candidates.append((not next_hop, index, order, e))
    return [e for *_, e in sorted(candidates, key=lambda c: c[:3])][:limit]


def error_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if ERROR_WORDS.search(line)]


def classify(caller_errors: int | None, target_errors: int | None) -> str:
    """오류 줄 수(None = 확인하지 못함)로 구간을 분류한다."""
    if caller_errors is None or target_errors is None:
        return UNCHECKED
    if caller_errors and target_errors:
        return BOTH
    if caller_errors:
        return CALLER_ONLY
    if target_errors:
        return TARGET_ONLY
    return NONE
