"""실행 추적: OpenTelemetry → Phoenix (실행 계획서 W2).

LangChain·LangGraph 호출을 OpenInference 계측으로 span을 만든다 (LLM 호출·도구 호출·토큰).
OTEL_EXPORTER_OTLP_ENDPOINT가 비어 있으면 아무것도 하지 않는다 (CI·단위 테스트).
로컬은 Phoenix를 port-forward한 http://localhost:6006/v1/traces (환경 설치 가이드 10단계).
"""

from core.config import Settings, get_setting

_configured = False


def setup_tracing(settings: Settings | None = None) -> bool:
    """추적을 켠다. 켰으면 True. 여러 번 불러도 한 번만 설정한다."""
    global _configured
    settings = settings or get_setting()
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return False
    if not _configured:
        _install(endpoint, settings.APP_NAME)
        _configured = True
    return True


def _install(endpoint: str, service_name: str) -> None:
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    LangChainInstrumentor().instrument(tracer_provider=provider)
