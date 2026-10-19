# 배포 직후 실패

## 한 줄 설명

새 버전 배포·설정 변경 직후 Pod가 뜨지 않거나, 뜬 뒤 일부 기능이 실패할 때 쓴다.

## 진입 조건

- 최근 롤아웃·ReplicaSet 변경이 있고 그 뒤로 실패가 시작됐다
- 규칙 P03·P06·P07·W01·W02·C01 중 하나가 적발됐거나 관찰 중이다
- 해당하지 않음: 배포 변경 없이 갑자기 실패 → app-layer-failure / service-unreachable

## 먼저 확인할 사실 (가설 없이)

| 확인할 사실 | 진단 API | 이 결과가 의미하는 것 |
|---|---|---|
| 새·이전 ReplicaSet의 이미지·Ready 수 | `get_resource` (Deployment·ReplicaSet) | 새 버전만 Ready 0이면 새 버전 문제, 이전 버전이 아직 트래픽을 받는지 |
| Pod 상태 사유 | `get_resource` (Pod) | `ImagePullBackOff`·`CrashLoopBackOff`·`CreateContainerConfigError`·`OOMKilled` |
| 경고 이벤트 | `get_events` | 이미지 받기·설정 참조·스케줄 실패의 직접 메시지 |
| 직전 컨테이너 로그 | `get_pod_logs` (previous) | 기동 중 예외(설정·DB 연결·마이그레이션) |

## 원인 후보와 판별법

| 원인 후보 | 지지 근거 | 배제 근거 | 확인에 쓸 진단 API |
|---|---|---|---|
| 이미지 받기 실패 | `ErrImagePull`·`ImagePullBackOff`, 이벤트의 태그·인증 오류 | 컨테이너가 한 번이라도 시작됨 | `get_resource`, `get_events` |
| 설정·Secret 참조 누락 | `CreateContainerConfigError`, 이벤트에 없는 ConfigMap·키 | 참조 객체·키가 모두 있음 | `get_resource`, `get_events` |
| 기동 중 예외 (설정값·의존 서비스) | CrashLoop, previous 로그의 예외 | 재시작 없음 | `get_pod_logs` |
| 메모리 부족 | 종료 사유 `OOMKilled`, limits 대비 사용량 | 종료 사유가 다름 | `get_resource` |
| DB 스키마·데이터 불일치 | 새 버전에서만 특정 API 500, 로그의 컬럼·테이블 오류 | 이전 버전과 같은 API가 같은 결과 | `get_pod_logs`, `run_api_scenario` |
| 롤아웃 정체 (리소스·스케줄) | 새 Pod `Pending`, 스케줄 실패 이벤트 | 새 Pod가 스케줄됨 | `get_events` |

- 판정 기준: 서로 다른 출처의 근거 2개 이상으로 지지되고 나머지 후보가 배제되면 확정한다.

## 흔한 함정

- 롤아웃이 멈춰도 이전 버전 Pod가 Ready로 남아 서비스는 동작할 수 있다. "서비스는 되는데 배포가 안 끝남"과 "서비스 장애"를 구분한다.
- 같은 이미지를 다른 Pod가 받았는지로 레지스트리 장애와 태그 오류를 가른다.
- 새 버전이 떠 있어도 DB 마이그레이션이 없으면 특정 API만 실패한다. Pod 상태만 보고 정상으로 판단하지 않는다.

## 확정하지 못할 때

- 레지스트리 인증·네트워크는 클러스터 밖이라 직접 확인할 수 없다.
- Secret 값은 읽지 않는다. 키 존재까지만 확인할 수 있다.

## 권장 조치 방향

| 원인 후보 | 조치 방향 | 재검증 방법 |
|---|---|---|
| 이미지 받기 실패 | 태그·레지스트리 주소·pull Secret 확인, 필요하면 이전 버전으로 롤백 | 새 Pod Running |
| 설정 참조 누락 | 빠진 ConfigMap·Secret·키를 만들거나 참조를 고친다 | `CreateContainerConfigError` 사라짐 |
| 기동 중 예외·스키마 불일치 | 이전 버전으로 롤백 후 원인 수정 (마이그레이션 적용 등) | 실패하던 API 재호출 |

## 출처

- kubernetes-mixin `KubePodCrashLooping`·`KubeDeploymentRolloutStuck` 런북의 원인 목록
- Kubernetes [Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)
