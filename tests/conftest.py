import pytest

from core.config import get_setting


@pytest.fixture(autouse=True)
def _no_tracing_export(monkeypatch):
    """로컬 .env의 OTEL_EXPORTER_OTLP_ENDPOINT를 읽지 않게 한다 (CI와 같은 조건)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    get_setting.cache_clear()
    yield
    get_setting.cache_clear()
