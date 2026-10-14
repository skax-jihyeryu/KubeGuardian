"""로거 설정."""

import logging
from functools import lru_cache

from core.config import get_setting


@lru_cache
def get_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger(get_setting().APP_NAME)
