"""지원하는 분·시간 주기의 표시와 점검 중단 기준."""

import re
from datetime import datetime, timedelta

_EVERY_MINUTES = re.compile(r"\*/(\d{1,2}) \* \* \* \*")


def interval(schedule: str) -> timedelta | None:
    if schedule == "0 * * * *":
        return timedelta(hours=1)
    match = _EVERY_MINUTES.fullmatch(schedule)
    if match and 0 < int(match[1]) <= 60 and 60 % int(match[1]) == 0:
        return timedelta(minutes=int(match[1]))
    return None


def stalled_after(schedule: str) -> timedelta:
    # 기존 정책대로 예정된 점검 세 번을 놓쳤을 때 중단으로 본다.
    return 3 * (interval(schedule) or timedelta(minutes=5))


def next_check(schedule: str, now: datetime) -> datetime | None:
    step = interval(schedule)
    if step is None:
        return None
    minutes = int(step.total_seconds() // 60)
    base = now.replace(second=0, microsecond=0)
    return base - timedelta(minutes=base.minute % minutes) + step
