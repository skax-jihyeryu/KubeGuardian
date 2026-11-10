"""API와 웹 화면이 함께 쓰는 FastAPI 의존성. 테스트는 app.dependency_overrides로 바꾼다."""

from datetime import UTC, datetime
from functools import lru_cache, partial

from sqlalchemy import Engine

from core.config import KubeGuardianConfig, get_setting, load_kubeguardian_config
from core.db import create_db_engine
from core.llm import build_chat_model
from service.agent.base import LlmFactory


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_setting().KG_DB_PATH)


def get_config() -> KubeGuardianConfig:
    return load_kubeguardian_config()


def get_llm_factory() -> LlmFactory:
    return build_chat_model


def get_now() -> datetime:
    """화면이 보는 '지금'. 테스트는 이 의존성을 바꾼다."""
    return datetime.now(UTC)


def get_evidence_summary_factory() -> LlmFactory:
    return partial(build_chat_model, timeout=30, max_retries=0)
