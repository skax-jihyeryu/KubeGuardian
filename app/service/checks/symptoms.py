"""증상 감지: 설정 `api_scenarios`를 모두 실행해 사용자 흐름이 깨졌는지 본다 (기획서 §7.3).

실행은 에이전트 도구 `RunApiScenario`를 그대로 쓴다 (마스킹·근거 저장이 같이 따라온다).
- 어떤 단계가 실패한 시나리오가 증상이다.
- 도구가 시나리오를 실행하지 못한 경우(계정 없음, Service·포트 못 읽음 등)는 증상이 아니라
  `unknown`(확인하지 못함)으로 남긴다. 사용자 흐름이 깨졌다는 근거가 없으니 게이트는 열지 않되
  표시한다. 단계에서 HTTP 연결이 실패한 것은 시나리오가 돌았으므로 증상(failed)이다.
- 설정 로드가 시나리오의 `feature`를 `core_paths` 키로 제한하므로 실패한 시나리오는 항상
  critical이다 (05-rules §3). warning은 사용자 증상만 있을 때 게이트가 정한다.
"""

from core.config import KubeGuardianConfig
from service.model import Symptom
from service.tools.app_api import RunApiScenario
from service.tools.base import ToolContext


def detect_symptoms(config: KubeGuardianConfig, ctx: ToolContext) -> list[Symptom]:
    """모든 시나리오를 설정 순서대로 실행한 결과 (passed·failed·unknown)."""
    tool = RunApiScenario(config.api_scenarios)
    out = []
    for scenario in config.api_scenarios:
        name = scenario.name
        result = tool.run({"name": name}, ctx)
        if not result.ok:
            out.append(
                Symptom(
                    scenario=name,
                    status="unknown",
                    summary=result.summary,
                    evidence_id=result.evidence_id,
                )
            )
            continue
        failed = result.data["failed_step"]
        severity = None if failed is None else "critical"
        out.append(
            Symptom(
                scenario=name,
                status="passed" if failed is None else "failed",
                failed_step=failed,
                summary=result.summary,
                severity=severity,
                evidence_id=result.evidence_id,
            )
        )
    return out
