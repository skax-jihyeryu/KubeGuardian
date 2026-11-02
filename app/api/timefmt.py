"""화면 시각 표시. DB는 UTC, 화면은 Asia/Seoul (D9). 템플릿 필터와 뷰모델이 같이 쓴다."""

from datetime import datetime, timedelta, timezone
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
