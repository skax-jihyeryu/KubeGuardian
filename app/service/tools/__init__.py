"""에이전트 도구 7종. sensors를 감싸며 에이전트 프레임워크에 묶이지 않는다."""

from core.config import KubeGuardianConfig
from service.tools.app_api import QueryAppApi, RunApiScenario
from service.tools.base import Tool
from service.tools.events import GetEvents
from service.tools.logs import GetPodLogs
from service.tools.probe import Probe
from service.tools.resource import GetResource, GetTopology


def build_tools(config: KubeGuardianConfig) -> list[Tool]:
    """에이전트에 등록하는 도구 목록. 설정(허용 목록·시나리오)은 생성자로 받는다."""
    return [
        GetTopology(config),
        GetResource(),
        GetPodLogs(),
        GetEvents(),
        Probe(),
        RunApiScenario(config.api_scenarios),
        QueryAppApi(config.app_apis),
    ]
