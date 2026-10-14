"""진단 에이전트용 LLM 생성.

어디에 연결할지(제공사·엔드포인트·키)는 설정이 정하고, 어떤 모델을 쓸지는
에이전트 버전의 config가 정한다. 제공사 분기는 이 파일 한 곳에만 둔다.

사내 SSL 프록시 CA는 코드가 아니라 환경 변수 SSL_CERT_FILE로 주입한다
(openai SDK의 httpx 클라이언트가 읽는다).
"""

from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI

from core.config import Settings, get_setting


def build_chat_model(
    model: str, *, temperature: float = 0, settings: Settings | None = None
) -> BaseChatModel:
    """model: Azure OpenAI에서는 배포 이름."""
    settings = settings or get_setting()

    if settings.LLM_PROVIDER == "azure_openai":
        return AzureChatOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            azure_deployment=model,
            temperature=temperature,
        )

    raise ValueError(f"지원하지 않는 LLM_PROVIDER: {settings.LLM_PROVIDER}")
