import logging
from datetime import UTC, datetime

from core.log.logging import get_logging


def test_http_client_loggers_are_quiet():
    get_logging()
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level == logging.WARNING


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
