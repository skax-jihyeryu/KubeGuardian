"""환경 설정. 값은 .env(로컬) 또는 Secret·환경 변수(클러스터)에서 읽는다."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
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

    # Slack 봇 토큰. 클러스터에서는 make kg-up이 .env의 선택 키를 kubeguardian-env Secret에 넣는다.
    # 비어 있으면 알림은 dry-run(알림 이력에만 기록)
    SLACK_BOT_TOKEN: str = ""

    # 실행 추적 (Phoenix, OpenTelemetry)
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    PHOENIX_PROJECT_NAME: str = "test"  # Phoenix 프로젝트 이름. 실서비스 전까지 test

    # 진단 대상·점검 설정 파일 (kubeguardian.example.yaml 참고)
    KG_CONFIG_PATH: str = "kubeguardian.yaml"
    # 시나리오용 테스트 계정 Secret을 마운트한 디렉터리 (<디렉터리>/<Secret 이름>/<키>)
    KG_ACCOUNTS_DIR: str = "/etc/kubeguardian/accounts"
    # 점검·이슈·대화 이력 SQLite 파일
    KG_DB_PATH: str = "kubeguardian.db"


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


# API 시나리오 단계 (run_api_scenario). JSON 경로는 점으로 잇고 목록 위치는 숫자로 쓴다
# (예: user.role.organizations.0.org_id).


class ResponseCheck(_Strict):
    """응답 JSON의 path 값이 문자열이고 not_starts_with로 시작하지 않아야 성공이다.

    템플릿 agent는 LLM 호출이 실패해도 HTTP 200에 "오류가 발생했습니다: ..."를 answer로 돌려준다.
    """

    path: str
    not_starts_with: str


class _ScenarioStepBase(_Strict):
    via: str  # 대상 네임스페이스의 Service 이름
    path: str
    port: int | None = None  # 없으면 Service의 첫 번째 포트
    timeout_s: float = 10.0
    expect: int | None = None  # 없으면 2xx를 성공으로 본다
    extract: dict[str, str] = {}  # 변수 이름 → 응답 JSON 경로. 뒤 단계 body에서 "${이름}"으로 쓴다


class LoginStep(_ScenarioStepBase):
    """account_secret Secret의 키·값(예: id, password)을 그대로 JSON 본문으로 POST한다."""

    account_secret: str
    token: str | None = None  # 토큰의 응답 JSON 경로. 없으면 access_token·accessToken·token


class CallStep(_ScenarioStepBase):
    method: Literal["GET", "POST"] = "POST"
    body: dict[str, Any] | None = None  # 값이 "${이름}"인 문자열은 앞 단계 extract 값으로 바꾼다
    check: ResponseCheck | None = None


class ApiScenarioStep(_Strict):
    """YAML에서 `- login: {...}` 또는 `- call: {...}` 한 키만 쓴다."""

    login: LoginStep | None = None
    call: CallStep | None = None

    @model_validator(mode="after")
    def _one_action(self) -> "ApiScenarioStep":
        if (self.login is None) == (self.call is None):
            raise ValueError("단계는 login·call 중 하나만 쓴다")
        return self


SCENARIO_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"  # run_api_scenario 도구 입력과 같다


class ApiScenario(_Strict):
    name: str = Field(pattern=SCENARIO_NAME_PATTERN)
    feature: str  # 이 시나리오가 지키는 사용자 기능. core_paths의 키 (심각도 판정, 05-rules §3)
    steps: list[ApiScenarioStep]


_DURATION = r"^\d+[mh]$"  # 30m, 2h


class ReminderPolicy(_Strict):
    first_after: str = Field(pattern=_DURATION)
    then_every: str | None = Field(default=None, pattern=_DURATION)
    max: int = Field(ge=1)  # 리마인더 총 횟수


DEFAULT_REPORT_URL = "http://localhost:8000/p/default"


class SlackConfig(_Strict):
    alert_target: str
    bot_token_secret: str
    app_token_secret: str
    allowed_channels: list[str] = []
    allowed_users: list[str] = []
    reminders: dict[Literal["critical", "warning"], ReminderPolicy] = {}  # 심각도별 재알림
    # 알림 링크의 보고서 화면 주소 (UI 정의서 §1.2: <report_url>/checks/{run}, /issues/{id})
    report_url: str = DEFAULT_REPORT_URL


class KnowledgeConfig(_Strict):
    runbooks: list[str] = []
    environment: str | None = None


class KubeGuardianConfig(_Strict):
    target: TargetConfig
    schedule: str = Field(default="*/5 * * * *", pattern=r"^\S+( \S+){4}$")  # cron 5필드
    core_paths: dict[str, list[str]] = {}  # 사용자 기능 → 서비스 목록 (1차 심각도, 05-rules §3)
    external_hosts: list[str] = []  # S04 제외: 외부 호스트 허용 목록
    required_config: dict[str, list[str]] = {}  # C05: 서비스 → 반드시 있어야 할 env 이름
    topology: TopologyConfig = TopologyConfig()
    api_scenarios: list[ApiScenario] = []
    app_apis: dict[str, list[str]] = {}  # query_app_api 허용 목록 (GET만)
    # 수집에서 뺄 이벤트의 대상 API 그룹 (involvedObject.apiVersion의 그룹, 정확히 일치).
    # 평가용 장애 주입 도구의 흔적이 점검·조사 입력에 들어가지 않게 한다 (#82). 운영 기본값은 비움
    exclude_event_api_groups: list[str] = []
    slack: SlackConfig | None = None
    knowledge: KnowledgeConfig = KnowledgeConfig()

    @model_validator(mode="after")
    def _scenarios_are_consistent(self) -> "KubeGuardianConfig":
        names = [s.name for s in self.api_scenarios]
        if len(names) != len(set(names)):
            raise ValueError("api_scenarios 이름 중복")
        unknown = sorted({s.feature for s in self.api_scenarios} - set(self.core_paths))
        if unknown:
            raise ValueError(f"api_scenarios feature가 core_paths에 없음: {unknown}")
        return self


def load_kubeguardian_config(path: str | Path | None = None) -> KubeGuardianConfig:
    path = Path(path or get_setting().KG_CONFIG_PATH)
    return KubeGuardianConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
