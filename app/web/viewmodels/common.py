"""웹 화면 모델에서 공유하는 표시 값과 변환."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Sev:
    key: str  # crit / warn / risk / tip
    label: str


def evidence_numbers(ids: list[Any]) -> list[int]:
    out = []
    for ev in ids:
        if isinstance(ev, str) and ev.startswith("ev-") and ev[3:].isdigit():
            out.append(int(ev[3:]))
    return out


def limited_names(names: list[str], limit: int) -> tuple[str, ...]:
    if len(names) <= limit:
        return tuple(names)
    return (*names[:limit], f"외 {len(names) - limit}개")
