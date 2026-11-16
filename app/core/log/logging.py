"""로거 설정."""

import logging
import re
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


class AccessLogFilter(logging.Filter):
    """uvicorn 접속 로그에서 상태 확인·정적 파일·화면 자동 갱신(폴링) 요청 줄을 뺀다.

    사람이 연 화면 요청은 남긴다. 자동 갱신은 `?live=1`, `/shell`, 근거 요약 GET이다.
    """

    QUIET_PREFIXES = ("/healthz", "/readyz", "/static/")
    SUMMARY_POLL = re.compile(r"/evidence/\d+/summary$")

    @classmethod
    def _is_polling(cls, method: object, target: str) -> bool:
        path, _, query = target.partition("?")
        return (
            "live=1" in query.split("&")
            or path.endswith("/shell")
            or (method == "GET" and bool(cls.SUMMARY_POLL.search(path)))
        )

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access의 args: (클라이언트 주소, 메서드, 경로, HTTP 버전, 상태 코드)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            return not (
                args[2].startswith(self.QUIET_PREFIXES) or self._is_polling(args[1], args[2])
            )
        return True


def format_uvicorn_logs() -> None:
    """uvicorn 자체 줄(서버 시작·오류·접속)도 같은 형식으로 찍고, 접속 줄에서 상태 확인 요청을 뺀다.

    uvicorn 로거는 자기 핸들러를 쓰고 root로 전파하지 않는다. uvicorn은 로깅 설정을
    앱 import보다 먼저 하므로, 앱 시작 시점에 그 핸들러의 formatter를 바꾸면 유지된다.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(KgFormatter())
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in access.filters):
        access.addFilter(AccessLogFilter())


@lru_cache
def get_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(KgFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # 요청마다 INFO 한 줄이라 흐름 로그를 묻는다
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(get_setting().APP_NAME)
