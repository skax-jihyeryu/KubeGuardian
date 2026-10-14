"""환경 설정. 값은 .env(로컬) 또는 Secret·환경 변수(클러스터)에서 읽는다."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "kubeguardian"
    APP_PORT: int = 8000

    # 진단 에이전트용 LLM. 제공사 분기는 core/llm.py, 모델 이름은 에이전트 버전 config가 정한다
    LLM_PROVIDER: str = "azure_openai"

    # Azure OpenAI (진단 대상 llm-gateway와 별도 배포)
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_API_VERSION: str = ""

    # 진단 대상
    KG_TARGET_NAMESPACE: str = "dev"

    # 실행 추적 (Phoenix, OpenTelemetry)
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""


@lru_cache
def get_setting() -> Settings:
    return Settings()
