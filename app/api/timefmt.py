"""화면 시각 표시. DB는 UTC, 화면은 Asia/Seoul (D9). 템플릿 필터와 뷰모델이 같이 쓴다."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    KST = ZoneInfo("Asia/Seoul")
except (
    ZoneInfoNotFoundError
):  # tzdata가 없는 최소 이미지. 한국은 서머타임이 없어 고정 +9시간과 같다
    KST = timezone(timedelta(hours=9), "KST")


def kst(value: datetime) -> datetime:
    return value.astimezone(KST)


def hm(value: datetime) -> str:
    return kst(value).strftime("%H:%M")


def hms(value: datetime) -> str:
    return kst(value).strftime("%H:%M:%S")


def md_hm(value: datetime) -> str:
    return kst(value).strftime("%m/%d %H:%M")


def seconds_text(seconds: float) -> str:
    """48초, 2분 5초."""
    total = max(0, int(seconds))
    return f"{total}초" if total < 60 else f"{total // 60}분 {total % 60}초"


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_date(raw: object) -> str:
    """엄격한 YYYY-MM-DD만 그대로 돌려주고, 아니면 빈 문자열."""
    if not isinstance(raw, str) or not _DATE.fullmatch(raw):
        return ""
    try:
        date.fromisoformat(raw)
    except ValueError:
        return ""
    return raw


def date_range(raw_from: object, raw_to: object) -> tuple[str, str]:
    """기간 입력 둘을 검증한다. 잘못된 값은 비우고, 시작이 끝보다 늦으면 둘 다 비운다."""
    start, end = parse_date(raw_from), parse_date(raw_to)
    return ("", "") if start and end and start > end else (start, end)


def date_window(start: str, end: str) -> tuple[datetime | None, datetime | None]:
    """검증된 KST 날짜 범위를 [시작 00:00, 끝 날짜 23:59:59.999999] 시각으로 바꾼다 (양끝 포함)."""
    since = datetime.combine(date.fromisoformat(start), time.min, KST) if start else None
    until = (
        datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, KST)
        - timedelta(microseconds=1)
        if end
        else None
    )
    return since, until
