import pytest
from langchain_openai import AzureChatOpenAI

from core.config import Settings
from core.llm import build_chat_model


def _azure_settings(**overrides) -> Settings:
    values = {
        "LLM_PROVIDER": "azure_openai",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "test-key",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_azure_provider_uses_model_as_deployment():
    llm = build_chat_model("gpt-4.1", settings=_azure_settings())

    assert isinstance(llm, AzureChatOpenAI)
    assert llm.deployment_name == "gpt-4.1"
    assert llm.azure_endpoint == "https://example.openai.azure.com/"
    assert llm.openai_api_version == "2024-10-21"


def test_temperature_defaults_to_zero_for_reproducible_diagnosis():
    llm = build_chat_model("gpt-4.1", settings=_azure_settings())

    assert llm.temperature == 0


def test_temperature_none_is_not_sent():
    llm = build_chat_model("gpt-4.1", temperature=None, settings=_azure_settings())

    assert llm.temperature is None
    assert "temperature" not in llm._default_params


def test_model_options_drop_temperature_and_use_responses_api():
    """gpt-6.1-sol: temperature 0을 거부하고, 도구 호출은 Responses API에서만 된다."""
    llm = build_chat_model("gpt-6.1-sol", settings=_azure_settings())

    assert llm.temperature is None
    assert llm.use_responses_api is True


def test_unlisted_model_keeps_defaults():
    llm = build_chat_model("gpt-4.1", settings=_azure_settings())

    assert llm.temperature == 0
    assert not llm.use_responses_api


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        build_chat_model("gpt-4.1", settings=_azure_settings(LLM_PROVIDER="unknown"))
