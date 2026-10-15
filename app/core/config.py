"""환경 설정. 값은 .env(로컬) 또는 Secret·환경 변수(클러스터)에서 읽는다."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "kubeguardian"
    APP_PORT: int = 8000

    # 진단 에이전트용 LLM. 제공사 분기는 infra/llm.py, 모델 이름은 에이전트 버전 config가 정한다
    LLM_PROVIDER: str = "azure_openai"

    # Azure OpenAI (진단 대상 llm-gateway와 별도 배포)
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_API_VERSION: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = ""

    # 진단 대상
    KG_TARGET_NAMESPACE: str = "dev"

    # 실행 추적 (Phoenix, OpenTelemetry)
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # 진단 대상·점검 설정 파일 (kubeguardian.example.yaml 참고)
    KG_CONFIG_PATH: str = "kubeguardian.yaml"


@lru_cache
def get_setting() -> Settings:
    return Settings()


# ── kubeguardian.yaml: 진단 대상·점검 설정 (상세설계 §7.4). 비밀값은 두지 않는다 ──


class _Strict(BaseModel):
    """오타 키를 조용히 무시하지 않도록 모르는 키는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class TargetConfig(_Strict):
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")  # DNS label


class TopologyConfig(_Strict):
    url_env_patterns: list[str] = ["*_SERVICE_URL", "*_URL"]


class ApiScenario(_Strict):
    name: str
    steps: list[dict[str, Any]]  # 단계 형식은 증상 감지(W2)에서 정한다


_DURATION = r"^\d+[mh]$"  # 30m, 2h


class ReminderPolicy(_Strict):
    first_after: str = Field(pattern=_DURATION)
    then_every: str | None = Field(default=None, pattern=_DURATION)
    max: int = Field(ge=1)  # 리마인더 총 횟수


class SlackConfig(_Strict):
    alert_target: str
    bot_token_secret: str
    app_token_secret: str
    allowed_channels: list[str] = []
    allowed_users: list[str] = []
    reminders: dict[Literal["critical", "warning"], ReminderPolicy] = {}  # 심각도별 재알림


class KnowledgeConfig(_Strict):
    runbooks: list[str] = []
    environment: str | None = None


class KubeGuardianConfig(_Strict):
    target: TargetConfig
    schedule: str = Field(default="*/5 * * * *", pattern=r"^\S+( \S+){4}$")  # cron 5필드
    core_paths: dict[str, list[str]] = {}  # 사용자 기능 → 서비스 목록 (1차 심각도, 05-rules §3)
    external_hosts: list[str] = []  # S04 제외: 외부 호스트 허용 목록
    topology: TopologyConfig = TopologyConfig()
    api_scenarios: list[ApiScenario] = []
    app_apis: dict[str, list[str]] = {}  # query_app_api 허용 목록 (GET만)
    slack: SlackConfig | None = None
    knowledge: KnowledgeConfig = KnowledgeConfig()


def load_kubeguardian_config(path: str | Path | None = None) -> KubeGuardianConfig:
    path = Path(path or get_setting().KG_CONFIG_PATH)
    return KubeGuardianConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
