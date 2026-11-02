"""점검 구성의 정의와 저장된 결과로 가는 링크. 클러스터는 조회하지 않는다."""

from dataclasses import dataclass
from datetime import datetime

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.config import KubeGuardianConfig
from core.db import RuleSelectionStore
from core.db.tables import CheckResultRow, CheckRunRow
from core.guard.redact import redact_mapping
from service.checks.detectors.catalog import CATALOG

CANDIDATES = {
    "N02": "노드 압박",
    "N03": "CoreDNS 비정상",
    "P01": "스케줄 실패",
    "P02": "PVC Pending",
    "P04": "FailedMount",
    "P05": "init 실패",
    "P08": "NotReady 지속",
    "P09": "Evicted",
    "C02": "같은 tag의 이미지 불일치",
    "C03": "ConfigMap 변경 후 미재시작",
    "C04": "ConfigMap 복수 적용",
}
STAGES = [
    ("0 환경", ["N01", "N02", "N03"]),
    ("1 빌드", ["C02", "P03"]),
    ("2 설정", ["C01", "C03", "C04", "C05"]),
    ("3 배포", ["W01", "W02"]),
    ("4 스케줄", ["P01", "P02"]),
    ("5 구동", ["P04", "P05", "P06", "P07", "P08", "P09"]),
    ("6 연결", ["S01", "S02", "S03"]),
    ("7 의존", []),
    ("8 요청", []),
    ("9 점검", []),
    ("복합 · 설정 + 연결", ["S04"]),
    ("개선 권고", ["H01", "H02"]),
]
KINDS = [
    ("Node", ["N01", "N02", "N03"]),
    ("Pod", [f"P{i:02}" for i in range(1, 10)]),
    ("Workload", ["W01", "W02"]),
    ("Service", ["S01", "S02", "S03", "S04"]),
    ("Config", [f"C{i:02}" for i in range(1, 6)]),
    ("개선 권고", ["H01", "H02"]),
]
SEVERITIES = {"critical": "긴급", "warning": "주의", "info": "개선 권고"}
NATURES = {"state": "현재 상태", "declared": "선언한 설정과 실제 구성", "time": "시간에 따른 변화"}

# docs/05-rules.md §4의 판정 조건과 출처를 화면용으로 요약한다.
RULE_DETAILS = {
    "N01": (
        "대상 Pod가 올라간 노드의 Ready 상태가 True가 아님. cordon한 노드는 제외합니다.",
        "kubernetes-mixin KubeNodeNotReady · for: 15m",
        "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodenotready/",
    ),
    "P03": (
        "컨테이너 대기 사유가 ErrImagePull, ImagePullBackOff, ErrImageNeverPull, "
        "InvalidImageName 중 하나입니다.",
        "kubernetes-mixin KubeContainerWaiting · for: 1h / Kubernetes Debug Pods",
        "https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/",
    ),
    "P06": (
        "CrashLoopBackOff이거나 직전 점검보다 restartCount가 증가했습니다. "
        "현재 Ready이고 재시작이 늘지 않은 과거 이력은 제외합니다.",
        "kubernetes-mixin KubePodCrashLooping · for: 15m; restartCount 증가로 관측 보정",
        "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping/",
    ),
    "P07": (
        "lastState.terminated.reason이 OOMKilled이고 종료 시각이 최근 15분 안입니다.",
        "설계값 · kubernetes-mixin 대응 규칙 없음 (15분 기준은 평가 후 조정)",
        None,
    ),
    "W01": (
        "Deployment의 spec.replicas가 availableReplicas보다 크고 updatedReplicas가 "
        "10분 동안 바뀌지 않았습니다. 이후 15분 지속해야 하므로 첫 관측 약 25분 뒤 적발합니다.",
        "kubernetes-mixin KubeDeploymentReplicasMismatch · for: 15m",
        "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/"
        "kubedeploymentreplicasmismatch/",
    ),
    "W02": (
        "Deployment의 Progressing 조건이 False입니다 (ProgressDeadlineExceeded). "
        "이전 버전 Pod가 Ready로 남아 있으면 1차 심각도는 주의입니다.",
        "kubernetes-mixin KubeDeploymentRolloutStuck · for: 15m / Kubernetes Deployment",
        "https://kubernetes.io/docs/concepts/workloads/controllers/deployment/",
    ),
    "S01": (
        "Service selector에 맞는 Pod가 네임스페이스에 0개입니다. "
        "selector가 없는 Service는 제외합니다.",
        "Kubernetes Debug Services · selector와 Pod 라벨 비교",
        "https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/",
    ),
    "S02": (
        "EndpointSlice의 Ready 엔드포인트가 없거나 전체보다 적습니다. "
        "종료 중·대상 Pod 0개·이전 버전이 서비스 중인 롤아웃의 새 Pod는 제외합니다.",
        "kubernetes-mixin KubePodNotReady · for: 15m / Kubernetes EndpointSlice",
        "https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/",
    ),
    "S03": (
        "Service targetPort가 대상 Pod의 컨테이너 포트에 없습니다. 이름 포트는 이름으로 "
        "비교하며, 숫자 포트인데 컨테이너 포트 선언 자체가 없으면 확인하지 못함입니다.",
        "Kubernetes Debug Services · port와 targetPort 비교",
        "https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/",
    ),
    "S04": (
        "URL 설정의 호스트가 localhost·127.0.0.1이거나 존재하지 않는 Service·포트입니다. "
        "허용 외부 호스트와 같은 Pod의 사이드카 포트는 제외하며 Secret 값은 읽지 않습니다.",
        "운영 사례 · 템플릿 서비스 주소 결함 T1 (외부 표준 없음)",
        None,
    ),
    "C01": (
        "Pod의 envFrom·valueFrom·volume이 참조하는 ConfigMap·Secret 객체나 키가 없습니다. "
        "optional: true 참조는 제외하며 Secret은 키 이름만 확인합니다.",
        "Kubernetes ConfigMap 사용 문서; Secret 공식 문구는 미확인",
        "https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/",
    ),
    "C05": (
        "required_config에 선언한 env 이름이 서비스 컨테이너의 env·envFrom 어디에도 없습니다. "
        "빈 값도 존재로 보며, 선언한 서비스 자체가 없어도 설정 오류로 적발합니다.",
        "운영자 선언 required_config · C01 확장 (설계 결정 #60)",
        "https://github.com/skax-jihyeryu/KubeGuardian/issues/60",
    ),
    "H01": (
        "Service가 선택하는 Pod의 컨테이너에 readinessProbe가 없거나 tcpSocket만 사용합니다. "
        "개선 권고로만 남기며 조사 게이트와 알림을 열지 않습니다.",
        "Polaris readinessProbeMissing / Kubernetes Probes",
        "https://kubernetes.io/docs/tasks/configure-pod-container/"
        "configure-liveness-readiness-startup-probes/",
    ),
    "H02": (
        "컨테이너 resources.requests 또는 resources.limits의 cpu·memory 중 하나라도 없습니다. "
        "개선 권고로만 남깁니다.",
        "Polaris 리소스 검사 항목 · 해당 출처 페이지 미확인",
        None,
    ),
}


@dataclass(frozen=True)
class RuleDefinition:
    id: str
    name: str
    enabled: bool
    available: bool
    hold: str
    grade: str
    severities: str
    nature: str
    stage: str
    kind: str
    predicate: str
    source: str
    source_url: str | None


def rule_groups(engine: Engine, view: str):
    disabled = RuleSelectionStore(engine).disabled()
    stage_of = {r: name for name, ids in STAGES for r in ids}
    kind_of = {r: name for name, ids in KINDS for r in ids}
    definitions = {}
    for rid, name in {**CANDIDATES, **{k: r.name for k, r in CATALOG.items()}}.items():
        spec = CATALOG.get(rid)
        minutes = int(spec.hold.total_seconds() / 60) if spec and spec.hold else 0
        hold = f"{minutes // 60}시간" if minutes and minutes % 60 == 0 else f"{minutes}분"
        definitions[rid] = RuleDefinition(
            rid,
            name,
            rid not in disabled and spec is not None,
            spec is not None,
            hold if minutes else "즉시",
            "개선 권고" if rid.startswith("H") else "장애 신호",
            " / ".join(SEVERITIES[s] for s in spec.severities) if spec else "준비 중",
            NATURES[spec.nature] if spec else "준비 중",
            stage_of[rid],
            kind_of[rid],
            *RULE_DETAILS.get(rid, ("준비 중", "아직 구현되지 않은 규칙", None)),
        )
    groups = [
        (name, [definitions[r] for r in ids]) for name, ids in (KINDS if view == "kind" else STAGES)
    ]
    return groups, definitions


def yaml_text(value) -> str:
    return yaml.safe_dump(redact_mapping(value), allow_unicode=True, sort_keys=False).strip()


def services(config: KubeGuardianConfig) -> list[str]:
    names = {name for path in config.core_paths.values() for name in path}
    names.update(config.required_config)
    names.update(config.app_apis)
    names.update((s.login or s.call).via for a in config.api_scenarios for s in a.steps)
    return sorted(names)


def recent_links(engine: Engine, *, rule: str | None = None, feature: str | None = None):
    """최신순으로 읽어 최근 적발·실패 최대 5건을 찾는다. 스냅샷은 읽지 않는다."""
    stmt = select(
        CheckResultRow.run_id,
        CheckResultRow.verdicts,
        CheckResultRow.symptoms,
        CheckRunRow.started_at,
    ).join(CheckRunRow, CheckRunRow.id == CheckResultRow.run_id)
    matches: list[tuple[int, datetime]] = []
    with Session(engine) as session:
        for row in session.execute(
            stmt.order_by(CheckResultRow.run_id.desc()).execution_options(yield_per=50)
        ):
            if rule and any(
                v.get("rule_id") == rule
                and any(i.get("verdict") == "detected" for i in v.get("items", []))
                for v in row.verdicts or []
            ):
                matches.append((row.run_id, row.started_at))
            if feature and any(
                s.get("scenario") == feature and s.get("status") == "failed"
                for s in row.symptoms or []
            ):
                matches.append((row.run_id, row.started_at))
            if len(matches) >= 5:
                break
    return matches


def service_details(config: KubeGuardianConfig, name: str):
    paths = [feature for feature, path in config.core_paths.items() if name in path]
    return redact_mapping(
        {
            "features": paths,
            "required": config.required_config.get(name, []),
            "apis": config.app_apis.get(name, []),
            "yaml": yaml_text(
                {
                    "core_paths": {k: v for k, v in config.core_paths.items() if name in v},
                    "required_config": {name: config.required_config.get(name, [])},
                }
            ),
        }
    )
