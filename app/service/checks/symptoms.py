"""증상 감지: 설정 `api_scenarios`를 모두 실행해 사용자 흐름이 깨졌는지 본다 (기획서 §7.3).

실행은 에이전트 도구 `RunApiScenario`를 그대로 쓴다 (마스킹·근거 저장이 같이 따라온다).
- 어떤 단계가 실패한 시나리오가 증상이다.
- 도구가 시나리오를 실행하지 못한 경우(Service 없음·수집 실패 등)는 증상으로 치지 않고
  결과에서 뺀다. 그 원인은 규칙이 따로 잡는다.
- 심각도는 시나리오의 `feature`가 `core_paths`의 키면 critical, 아니면 warning (05-rules §3).
  설정 로드가 feature를 core_paths 키로 제한하므로 실제로는 실패한 시나리오가 모두 critical이다.
"""

from core.config import KubeGuardianConfig
from service.model import Symptom
from service.tools.app_api import RunApiScenario
from service.tools.base import ToolContext


def detect_symptoms(config: KubeGuardianConfig, ctx: ToolContext) -> list[Symptom]:
    """모든 시나리오를 설정 순서대로 실행한 결과. 실행하지 못한 시나리오는 없다."""
    tool = RunApiScenario(config.api_scenarios)
    out = []
    for scenario in config.api_scenarios:
        name, feature = scenario.name, scenario.feature
        result = tool.run({"name": name}, ctx)
        if not result.ok:
            continue
        failed = result.data["failed_step"]
        severity = None
        if failed is not None:
            severity = "critical" if feature in config.core_paths else "warning"
        out.append(
            Symptom(
                scenario=name,
                failed_step=failed,
                summary=result.summary,
                severity=severity,
                evidence_id=result.evidence_id,
            )
        )
    return out
