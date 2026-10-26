"""등록된 도구 전체의 설명·입력 스키마가 상세설계 §3.2와 맞는지 확인한다."""

import pytest

from core.config import KubeGuardianConfig
from service.tools import build_tools

CONFIG = KubeGuardianConfig.model_validate(
    {"target": {"namespace": "dev"}, "app_apis": {"app-svc": ["/api/v1/status"]}}
)

# 도구 이름 → 입력 필드 (상세설계 §3.2. 네임스페이스는 ToolContext가 정하므로 입력에 없다)
EXPECTED_INPUTS = {
    "get_topology": set(),
    "get_resource": {"kind", "name", "label_selector"},
    "get_pod_logs": {"pod", "container", "previous", "tail", "grep"},
    "get_events": {"kind", "name", "since_minutes"},
    "probe": {"target", "path", "via", "times"},
    "run_api_scenario": {"name"},
    "query_app_api": {"app", "path"},
}

TOOLS = build_tools(CONFIG)


def test_registered_tools_match_design_table():
    assert {t.name: set(t.Input.model_fields) for t in TOOLS} == EXPECTED_INPUTS


def test_query_app_api_registered_only_when_app_apis_set():
    empty = KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}})
    assert "query_app_api" not in {t.name for t in build_tools(empty)}
    assert "query_app_api" in {t.name for t in TOOLS}


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_says_when_to_use(tool):
    assert tool.description.strip()
    assert "때 쓰세요" in tool.description


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_input_rejects_unknown_arguments(tool):
    assert tool.Input.model_config.get("extra") == "forbid"
