import logging

from core.log.logging import get_logging


def test_http_client_loggers_are_quiet():
    get_logging()
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level == logging.WARNING
