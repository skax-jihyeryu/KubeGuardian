"""로거 설정."""

import logging
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from core.config import get_setting

LOG_TZ = ZoneInfo("Asia/Seoul")  # 로그 시각은 운영자가 읽는 한국 시각


class KgFormatter(logging.Formatter):
    """`2026-11-16 18:35:58 INFO [worker] 메시지`. 역할 자리에 로거 이름 대신 KG_ROLE을 쓴다."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s [%(role)s] %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, LOG_TZ).strftime("%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        setting = get_setting()
        record.role = setting.KG_ROLE or setting.APP_NAME
        return super().format(record)


@lru_cache
def get_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(KgFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # 요청마다 INFO 한 줄이라 흐름 로그를 묻는다
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(get_setting().APP_NAME)
