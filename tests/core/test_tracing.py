from core import tracing
from core.config import Settings


def _settings(endpoint: str) -> Settings:
    return Settings(_env_file=None, OTEL_EXPORTER_OTLP_ENDPOINT=endpoint)


def test_tracing_is_noop_without_endpoint(monkeypatch):
    monkeypatch.setattr(tracing, "_configured", False)

    assert tracing.setup_tracing(_settings("")) is False


def test_tracing_setup_is_idempotent(monkeypatch):
    monkeypatch.setattr(tracing, "_configured", False)
    calls = []
    monkeypatch.setattr(tracing, "_install", lambda endpoint, name: calls.append(endpoint))

    assert tracing.setup_tracing(_settings("http://localhost:6006/v1/traces")) is True
    assert tracing.setup_tracing(_settings("http://localhost:6006/v1/traces")) is True
    assert calls == ["http://localhost:6006/v1/traces"]
