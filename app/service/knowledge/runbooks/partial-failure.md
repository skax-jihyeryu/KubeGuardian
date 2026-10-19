# 요청의 일부만 실패

## 한 줄 설명

같은 요청이 어떤 때는 성공하고 어떤 때는 실패할 때(간헐 실패, 일부 Pod만 이상) 쓴다.

## 진입 조건

- 같은 사용자 시나리오를 여러 번 돌리면 결과가 섞인다, 또는 사용자가 "가끔 안 된다"고 한다
- 규칙 S02(Ready 엔드포인트 일부)·N01·W01(일부)이 적발됐거나, 장애 신호 없이 증상만 있다
- 해당하지 않음: 매번 실패 → service-unreachable / app-layer-failure

## 먼저 확인할 사실 (가설 없이)

| 확인할 사실 | 진단 API | 이 결과가 의미하는 것 |
|---|---|---|
| 실패 비율 (여러 번 호출) | `run_api_scenario` 반복 | 약 1/N이면 N개 Pod 중 하나일 가능성 |
| Pod별 직접 호출 결과 | `probe` (Pod 지정) | 특정 Pod만 실패하면 그 Pod 문제 |
| Pod별 노드·재시작·리소스 | `get_resource` (Pod·Node) | 실패 Pod가 같은 노드에 몰렸는지 |
| 실패 Pod의 로그 | `get_pod_logs` | 그 Pod에만 있는 오류 |

## 원인 후보와 판별법

| 원인 후보 | 지지 근거 | 배제 근거 | 확인에 쓸 진단 API |
|---|---|---|---|
| 특정 Pod만 이상 (앱 오류) | 그 Pod 직접 호출만 실패, 그 Pod 로그에만 오류 | 모든 Pod 직접 호출 성공 | `probe`, `get_pod_logs` |
| 특정 노드 문제 | 실패 Pod가 한 노드에 몰림, 노드 조건 이상 | 노드가 섞여 있음 | `get_resource` (Node) |
| 리소스 부족·스로틀링 | 실패 Pod의 CPU·메모리가 limits에 붙음, 지연 증가 | 리소스 여유 | `get_resource` |
| 타임아웃 경계 | 실패가 느린 요청에만, 로그의 timeout | 실패가 빠르게 남 | `run_api_scenario`, `get_pod_logs` |
| 버전 혼재 (롤아웃 중) | Pod 이미지·ReplicaSet이 둘, 한 버전만 실패 | 모든 Pod가 같은 버전 | `get_resource` |

- 판정 기준: 서로 다른 출처의 근거 2개 이상으로 지지되고 나머지 후보가 배제되면 확정한다.

## 흔한 함정

- Service 경유 호출은 Pod를 고르게 돌지 않는다. 한두 번 성공했다고 정상이 아니다. Pod를 지정해 직접 호출한다.
- readinessProbe가 TCP면 앱 오류를 내는 Pod도 Ready로 남아 계속 트래픽을 받는다. "모든 Pod Ready"는 배제 근거가 아니다.
- 실패 비율이 낮으면 반복 횟수를 늘려야 보인다.

## 확정하지 못할 때

- 노드 하드웨어·커널 문제는 노드 조건과 이벤트로만 간접 확인한다.
- 실패가 재현되지 않으면 "재현 실패"로 적고 관찰을 이어 가도록 남긴다.

## 권장 조치 방향

| 원인 후보 | 조치 방향 | 재검증 방법 |
|---|---|---|
| 특정 Pod 이상 | 그 Pod를 삭제해 다시 만들고, readinessProbe를 앱 상태를 보는 HTTP 검사로 바꾼다 | 모든 Pod 직접 호출 성공 |
| 노드 문제 | 노드를 cordon·drain 하고 원인 확인 | 다른 노드에서 성공 |
| 리소스 부족 | requests·limits 조정, replicas 증설 | 지연·실패율 감소 |

## 출처

- Kubernetes [Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/): TCP 검사는 소켓 연결만 확인
- kubernetes-mixin `KubePodNotReady` 런북
