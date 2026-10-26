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
    monkeypatch.setattr(tracing, "_install", lambda endpoint, name, project: calls.append(endpoint))

    assert tracing.setup_tracing(_settings("http://localhost:6006/v1/traces")) is True
    assert tracing.setup_tracing(_settings("http://localhost:6006/v1/traces")) is True
    assert calls == ["http://localhost:6006/v1/traces"]


def test_tests_do_not_read_local_tracing_endpoint():
    # 로컬 .env의 추적 설정을 읽으면 테스트가 Phoenix로 span을 내보내려 한다
    from core.config import get_setting

    assert get_setting().OTEL_EXPORTER_OTLP_ENDPOINT == ""


def _installed_resource_attributes(monkeypatch, settings):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    captured = []
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: captured.append(p))
    monkeypatch.setattr(
        "openinference.instrumentation.langchain.LangChainInstrumentor.instrument",
        lambda self, **kw: None,
    )

    tracing.setup_tracing(settings)

    assert isinstance(captured[0], TracerProvider)
    return captured[0].resource.attributes


def test_phoenix_project_defaults_to_test(monkeypatch):
    attrs = _installed_resource_attributes(
        monkeypatch, _settings("http://localhost:6006/v1/traces")
    )

    assert attrs["openinference.project.name"] == "test"


def test_phoenix_project_uses_configured_name(monkeypatch):
    settings = Settings(
        _env_file=None,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:6006/v1/traces",
        PHOENIX_PROJECT_NAME="kubeguardian",
    )

    attrs = _installed_resource_attributes(monkeypatch, settings)

    assert attrs["openinference.project.name"] == "kubeguardian"
