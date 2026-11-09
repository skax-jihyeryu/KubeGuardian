"""호출 구간 근거표: 경로 B 사전 확인에서 실패 경로의 호출 구간마다 관찰 사실을 한 줄로 모은다.

구간(topology edge)마다 호출하는 쪽 로그, 받는 쪽 Pod Ready 수, 받는 쪽 오류 로그를 본다.
분류는 관찰한 오류 기록의 유무만으로 정하고 원인을 단정하지 않는다.
"""

import re

BOTH = "양쪽 오류"
CALLER_ONLY = "호출하는 쪽만 오류, 구간 불일치"
TARGET_ONLY = "받는 쪽만 오류"
UNCHECKED = "확인하지 못함"


def error_pattern(grep: str) -> re.Pattern[str]:
    """`a|b|429` 형태의 오류 키워드 목록으로 줄 판정 정규식을 만든다.

    상태 코드(숫자)는 다른 숫자에 걸리지 않게 경계를 본다: `1,503`·`03.502Z`·`id=7f4290`은 제외.
    """
    words = [w for w in grep.split("|") if w]
    text = [re.escape(w) for w in words if not w.isdigit()]
    codes = [re.escape(w) for w in words if w.isdigit()]
    parts = text + ([rf"(?<![\w,.])(?:{'|'.join(codes)})(?!\w|[,.]\d)"] if codes else [])
    return re.compile("|".join(parts), re.IGNORECASE)


def host_pattern(name: str) -> re.Pattern[str]:
    """줄에서 `name`이 URL 호스트 자리로 쓰인 경우: `//name`, `name:포트`, `name.네임스페이스`.

    `User-Agent`, `/api/v1/agent/`, `llm-gateway`의 gateway 같은 낱말·경로 조각은 제외한다.
    """
    n = re.escape(name)
    return re.compile(rf"(?<=//){n}(?![\w-])|(?<![\w./@-]){n}(?::\d+|\.[a-z0-9])", re.IGNORECASE)


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


def classify(caller_errors: int | None, target_errors: int | None, scope: str) -> str:
    """오류 줄 수(None = 확인하지 못함)로 구간을 분류한다. scope는 조회 범위 문구."""
    if caller_errors is None or target_errors is None:
        return UNCHECKED
    if caller_errors and target_errors:
        return BOTH
    if caller_errors:
        return CALLER_ONLY
    if target_errors:
        return TARGET_ONLY
    return f"{scope}에서 이 구간 오류 기록 없음"
