import io
import logging
import re
from datetime import UTC, datetime

import pytest

from core.log.logging import AccessLogFilter, get_logging


def test_http_client_loggers_are_quiet():
    get_logging()
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level == logging.WARNING


@pytest.fixture(autouse=True)
def _reset_setting_cache():
    from core.config import get_setting

    yield
    get_setting.cache_clear()


def _record(msg="메시지", level=logging.INFO, name="service.pipeline.worker", created=None):
    record = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    if created is not None:
        record.created = created
    return record


def _formatter(monkeypatch, role=None):
    from core.config import get_setting
    from core.log.logging import KgFormatter

    if role is None:
        monkeypatch.delenv("KG_ROLE", raising=False)
    else:
        monkeypatch.setenv("KG_ROLE", role)
    get_setting.cache_clear()
    return KgFormatter()


def test_time_is_shown_in_korea_time(monkeypatch):
    utc = datetime(2026, 11, 16, 9, 35, 58, tzinfo=UTC).timestamp()
    line = _formatter(monkeypatch, "worker").format(_record(created=utc))
    assert line.startswith("2026-11-16 18:35:58 ")


def test_line_has_level_and_role_instead_of_logger_name(monkeypatch):
    line = _formatter(monkeypatch, "worker").format(_record(level=logging.WARNING))
    assert line.endswith("WARNING [worker] 메시지")
    assert "service.pipeline" not in line


def test_role_falls_back_to_app_name(monkeypatch):
    line = _formatter(monkeypatch).format(_record())
    assert "INFO [kubeguardian] 메시지" in line


def test_third_party_logger_gets_same_role(monkeypatch):
    line = _formatter(monkeypatch, "listener").format(_record(name="slack_sdk.socket_mode"))
    assert "INFO [listener] 메시지" in line


def _access(path):
    # uvicorn.access 줄 형식: '%s - "%s %s HTTP/%s" %d'
    args = ("10.0.0.1:5000", "GET", path, "1.1", 200)
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", args, None)


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/static/app.css"])
def test_access_filter_drops_probe_and_static_requests(path):
    assert AccessLogFilter().filter(_access(path)) is False


@pytest.mark.parametrize("path", ["/llm-gateway/checks", "/", "/api/v1/checks"])
def test_access_filter_keeps_screen_requests(path):
    assert AccessLogFilter().filter(_access(path)) is True


def test_create_app_installs_access_filter_once():
    from main import create_app

    create_app()
    create_app()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogFilter) for f in filters) == 1


@pytest.fixture
def uvicorn_logging(monkeypatch):
    """uvicorn이 앱 import 전에 하는 로깅 설정(dictConfig)을 그대로 적용하고 끝나면 되돌린다."""
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        n: (list(lg.handlers), list(lg.filters), lg.propagate, lg.level)
        for n in names
        for lg in [logging.getLogger(n)]
    }
    logging.config.dictConfig(LOGGING_CONFIG)
    monkeypatch.setenv("KG_ROLE", "web")
    from core.config import get_setting

    get_setting.cache_clear()
    out = io.StringIO()  # 접속 줄은 stdout, 나머지는 stderr로 가므로 한 곳으로 모은다
    for n in names:
        for handler in logging.getLogger(n).handlers:
            handler.setStream(out)
    yield out
    for n, (handlers, filters, propagate, level) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers, lg.filters, lg.propagate = handlers, filters, propagate
        lg.setLevel(level)


def test_uvicorn_access_line_uses_kg_format_and_drops_healthz(uvicorn_logging):
    from main import create_app

    create_app()
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:44246", "GET", "/healthz", "1.1", 200)
    access.info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:44246", "GET", "/llm-gateway/checks", "1.1", 200
    )

    lines = uvicorn_logging.getvalue().strip().splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO \[web\] 127\.0\.0\.1:44246 - "
        r'"GET /llm-gateway/checks HTTP/1\.1" 200',
        lines[0],
    )


def test_uvicorn_server_and_error_lines_use_kg_format(uvicorn_logging):
    from main import create_app

    create_app()
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logging.getLogger("uvicorn.error").error("Exception in ASGI application")

    lines = uvicorn_logging.getvalue().strip().splitlines()
    assert [re.sub(r"^\S+ \S+ ", "", line) for line in lines] == [
        "INFO [web] Started server process [1]",
        "ERROR [web] Exception in ASGI application",
    ]
