"""문제 지문: AI 호출 전에 결정적 신호로 계산한다 (기획서 §6.6).

지문 = (게이트 경로, 적발된 감지기 ID 목록, 실패한 API 시나리오 단계, 대상 서비스).
같은 지문이 이어지면 같은 장애가 계속되는 것이고, 바뀌면(새 신호, 다른 단계) 상황이 바뀐 것이다.
Pod 이름처럼 재시작마다 바뀌는 값은 지문에 넣지 않는다. 해결 판정에 쓸 실제 대상은 targets에 둔다.

영향 기능(features)은 이슈를 묶는 기준이다: 신호의 서비스가 속한 core_paths 기능과 실패한 시나리오의
feature. 어느 기능에도 속하지 않으면 OTHER_FEATURE 하나로 둔다.
"""

import hashlib
import json
from dataclasses import dataclass

from core.config import KubeGuardianConfig
from service.model import GateResult

OTHER_FEATURE = "(기타)"


@dataclass(frozen=True)
class Fingerprint:
    path: str
    rule_ids: tuple[str, ...]
    failed_steps: tuple[str, ...]  # "<시나리오>:<단계>"
    services: tuple[str, ...]
    features: tuple[str, ...]
    targets: tuple[tuple[str, str], ...]  # (규칙 ID, 판정 대상) — 해결 판정용, 지문 밖
    scenarios: tuple[str, ...]  # 실패한 시나리오 이름 — 해결 판정용

    @property
    def key(self) -> str:
        body = [self.path, self.rule_ids, self.failed_steps, self.services]
        return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode()).hexdigest()


def compute_fingerprint(gate: GateResult, config: KubeGuardianConfig) -> Fingerprint:
    services = {s.service for s in gate.signals if s.service}
    scenario_feature = {x.name: x.feature for x in config.api_scenarios}
    features = {f for f, members in config.core_paths.items() if services & set(members)}
    features |= {
        scenario_feature[x.scenario] for x in gate.symptoms if x.scenario in scenario_feature
    }
    return Fingerprint(
        path=gate.path,
        rule_ids=tuple(sorted({s.rule_id for s in gate.signals})),
        failed_steps=tuple(sorted({f"{x.scenario}:{x.failed_step}" for x in gate.symptoms})),
        services=tuple(sorted(services)),
        features=tuple(sorted(features)) or (OTHER_FEATURE,),
        targets=tuple(sorted({(s.rule_id, s.subject) for s in gate.signals})),
        scenarios=tuple(sorted({x.scenario for x in gate.symptoms})),
    )
