"""진단 에이전트용 LLM 생성.

어디에 연결할지(제공사·엔드포인트·키)는 설정이 정하고, 어떤 모델을 쓸지는
에이전트 버전의 config가 정한다. 제공사 분기는 이 파일 한 곳에만 둔다.

사내 SSL 프록시 CA는 코드가 아니라 환경 변수 SSL_CERT_FILE로 주입한다
(openai SDK의 httpx 클라이언트가 읽는다).

모델마다 다른 API 제약은 MODEL_OPTIONS에 모은다. 모델을 바꿀 때는 에이전트 config의
이름을 바꾸고, 제약이 있는 모델이면 여기에 한 줄을 더한다.
"""

from typing import NamedTuple

from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI

from core.config import Settings, get_setting


class ModelOptions(NamedTuple):
    fixed_temperature: bool = False  # temperature를 지정할 수 없음 (기본값만 허용)
    responses_api: bool = (
        False  # 도구 호출이 Responses API에서만 됨 (api-version 2025-03-01-preview 이상)
    )


# 표에 없는 모델은 기본값 (temperature 지정 가능, chat completions)
MODEL_OPTIONS: dict[str, ModelOptions] = {
    "gpt-6.1-sol": ModelOptions(fixed_temperature=True, responses_api=True),
}


def build_chat_model(
    model: str, *, temperature: float | None = 0, settings: Settings | None = None
) -> BaseChatModel:
    """model: Azure OpenAI에서는 배포 이름.

    temperature=None이거나 MODEL_OPTIONS가 fixed_temperature면 temperature를 보내지 않는다.
    """
    settings = settings or get_setting()
    options = MODEL_OPTIONS.get(model, ModelOptions())
    if options.fixed_temperature:
        temperature = None

    if settings.LLM_PROVIDER == "azure_openai":
        return AzureChatOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            azure_deployment=model,
            temperature=temperature,
            use_responses_api=options.responses_api,
        )

    raise ValueError(f"지원하지 않는 LLM_PROVIDER: {settings.LLM_PROVIDER}")
