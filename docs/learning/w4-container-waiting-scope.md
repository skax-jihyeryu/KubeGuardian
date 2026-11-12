# W4 — 컨테이너 시작 대기 규칙 범위 (#68)

P03은 이미지 받기 실패 전용으로 유지한다. 이미지와 무관한 시작 대기는 별도 확장 후보로 두고, 아래 픽스처를 통과한 뒤 채택을 검토한다. 이번 스파이크의 산출물은 범위 결정과 검증 계획이다. 아래 픽스처는 후속 구현에서 실행할 검증 사례다.

## 판정 범위

- [P03 명세](../05-rules.md#p03--이미지-받기-실패)와 [구현](../../app/service/checks/detectors/p03.py)은 일반·초기화 컨테이너의 `state.waiting.reason` 중 `ErrImagePull`, `ImagePullBackOff`, `ErrImageNeverPull`, `InvalidImageName`을 1시간 기준으로 본다. 같은 컨테이너에서 이미지 사유끼리 바뀌어도 이미지 실패 관측은 이어진다. 이 범위를 유지하면 이미지 이름·태그·레지스트리 이벤트를 확인하라는 기존 진단이 관측 근거와 맞는다.
- Kubernetes의 `Waiting`은 컨테이너 시작에 필요한 작업이 남아 있는 상태다. Pod `Pending`에는 스케줄 대기까지 포함된다. P01(스케줄 실패)과 P02(PVC Pending)는 별도의 확장 후보다. PVC 바인딩이나 스케줄 상태는 각 자원에서 확인해야 한다. P04(FailedMount)와 P05(init 실패)도 각각 볼륨 마운트와 초기화의 별도 증거를 다루는 후보로 남긴다. [Kubernetes Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-states) · [점검 흐름과 후보](k8s-check-flow.html) · [설정 화면 후보 목록](../../app/web/viewmodels/setup.py)
- upstream `KubeContainerWaiting`은 일반 컨테이너의 `kube_pod_container_status_waiting_reason{reason!="CrashLoopBackOff", …} > 0`이 같은 시계열에서 매 평가마다 1시간 동안 성립하면 warning을 낸다. 초기화 컨테이너의 대기 사유는 별도 메트릭이다. [mixin 규칙 원본](https://github.com/kubernetes-monitoring/kubernetes-mixin/blob/master/alerts/apps_alerts.libsonnet) · [kube-state-metrics Pod 메트릭](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md) · [Prometheus 알림 규칙](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- KubeGuardian은 정시마다 API 스냅샷을 읽는다. 후보 조건이 최초 관측과 다음 정시 관측에서 모두 성립하면 첫 관측으로부터 약 1시간 뒤에 적발할 수 있다. 정시 점검이 제때 실행되고 상태가 유지된다면 실제 시작 후 약 1~2시간이다. 점검 사이의 회복·재발은 스냅샷에 나타나지 않을 수 있다. 직접 점검을 추가하면 그 시점의 상태도 확인할 수 있다. [현재 점검 정책](../05-rules.md) · [CronJob 설정](../../deploy/kubeguardian/cronjob.yaml)

## 별도 후보

첫 범위는 스케줄된 Pod의 일반 컨테이너에서 관측한 `ContainerCreating`, `CreateContainerConfigError`, `CreateContainerError`로 제한한다. 정상 시작에도 나타나는 `ContainerCreating`을 포함하므로 세 사유 모두 최소 1시간의 관측을 요구한다. Pod UID·컨테이너 이름·사유별로 시간을 쌓고, 사유가 바뀌면 새로 시작한다. 빈 사유, 다른 사유, 초기화 컨테이너는 이 후보 범위 밖이다. 이미지 실패는 P03, `CrashLoopBackOff`는 P06에서 계속 다룬다.

후보 신호에는 Pod·컨테이너·대기 사유·메시지·첫 관측 시각을 남긴다. `ContainerCreating`은 볼륨 마운트나 샌드박스·네트워크 준비 지연 중에도 나타날 수 있다. `CreateContainerConfigError`는 시작 구성 생성 실패, `CreateContainerError`는 컨테이너 생성 실패를 조사할 단서다. 구체 원인은 Pod 이벤트와 메시지, 관련 리소스 증거로 확인한다. 서비스 영향은 별도로 판단한다. [Kubernetes Pod 디버깅](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)

W02는 Deployment의 `Progressing=False`가 15분 관측되면 적발한다. 진행 기한 초과는 이 상태가 되는 한 사례다. S02는 Service의 Ready 엔드포인트 부족이 15분 관측되면 적발한다. 같은 Deployment의 이전 버전 Ready 엔드포인트가 남아 있으면 S02는 최신 ReplicaSet의 미준비 엔드포인트를 제외한다. Service와 Deployment가 없는 Pod에는 두 신호가 모두 없을 수 있다. C01은 필수 ConfigMap·Secret 참조 누락을 즉시 확인한다. 대기 중인 컨테이너나 그 컨테이너가 쓰는 볼륨에 해당하는 C01 근거가 있으면, 확인된 설정 문제와 대기 증상을 연결해 제시한다. W02·S02는 같은 장애에서 각각 배포와 트래픽 상태를 설명하는 증거로 쓴다. [규칙 명세](../05-rules.md)

## 채택 전 픽스처

고정 시각의 스냅샷을 사용하고 사례마다 지속 상태를 초기화한다. 아래 시간은 첫 관측부터의 경과다.

| 사례 | 입력 | 기대 결과 |
|---|---|---|
| 지속 | Service·Deployment 없는 스케줄된 Pod. 세 후보 사유를 각각 0분, 59분, 60분에 동일하게 관측 | 59분까지 미적발, 60분에 해당 사유로 적발. 근본 원인과 서비스 영향은 미확정 |
| 일시적 시작·재발 | 0분에 `ContainerCreating`, 5분 직접 점검에서 Running, 60분에 같은 사유로 재발 | 5분에 이전 관측 제거, 60분은 새 첫 관측으로 기록. 후보 미적발 |
| 중복 | 새 ReplicaSet의 컨테이너가 필수 ConfigMap을 env로 참조하고 `CreateContainerConfigError` 상태. Deployment `Progressing=False`와 새 버전 미준비 엔드포인트를 넣고 같은 Deployment의 이전 버전 Ready 엔드포인트 유무로 분기 | C01은 즉시 적발, W02는 15분에 적발, 후보는 60분에 C01 근거와 연결. S02는 이전 버전 Ready가 있으면 새 엔드포인트를 제외하고, 없으면 15분에 적발 |
| 범위 밖 | 미등록·빈 사유, 상태 없는 Pending Pod, 미스케줄 Pod, 초기화 컨테이너, 이미지 실패, CrashLoop를 각각 입력 | 후보 미적발, 이미지 실패·CrashLoop는 기존 규칙에 따름 |
| 확인 실패 | 0분에 후보 사유, 30분에 Pod 수집 실패, 60분에 같은 사유 재관측 | 30분에 확인 불가로 기록. 60분은 새 첫 관측으로 기록하고 120분에 같은 사유가 유지되면 적발 |
| 정리·재시작 | 기존 대기 Pod가 종료·삭제되거나 같은 이름의 새 UID로 교체. 별도 사례에서 동일 컨테이너가 다른 후보 사유로 전환 | 이전 관측 시간 제거. 새 UID나 새 사유의 첫 관측부터 다시 계산 |
