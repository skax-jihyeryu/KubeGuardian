"""API 의존성. 테스트는 app.dependency_overrides로 바꾼다."""

from functools import lru_cache

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
