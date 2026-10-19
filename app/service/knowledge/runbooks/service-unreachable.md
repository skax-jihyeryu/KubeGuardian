# 하위 서비스 호출 실패 (502·503·연결 거부)

## 한 줄 설명

요청을 중계하는 서비스가 하위 서비스를 부르지 못해 502·503·연결 거부·DNS 실패가 날 때 쓴다.

## 진입 조건

- 사용자 시나리오가 502·503·504로 실패하거나, 로그에 `connection refused`·`name resolution`·`upstream` 오류가 있다
- 규칙 S01·S02·S03·S04 중 하나가 적발됐다
- 해당하지 않음: 응답은 오지만 4xx(인증·입력 오류) → auth-failure / app-layer-failure, 일부 요청만 실패 → partial-failure

## 먼저 확인할 사실 (가설 없이)

| 확인할 사실 | 진단 API | 이 결과가 의미하는 것 |
|---|---|---|
| 실패한 단계와 상태 코드 | `run_api_scenario` | 첫 단계(로그인)부터 실패하면 중계 서비스 자체 또는 그 주소 설정 쪽 |
| 호출 관계와 주소 상태 | `get_topology` | `unresolved`(localhost·없는 Service·없는 포트) 엣지가 있으면 주소 설정 쪽 |
| 하위 Service의 엔드포인트 수 | `get_resource` (EndpointSlice) | 0이면 Service가 Pod를 고르지 못함, 있으면 경로·포트 쪽 |
| 중계 서비스 로그의 호출 대상 | `get_pod_logs` | 실제로 어느 호스트·포트로 호출했는지 |

## 원인 후보와 판별법

| 원인 후보 | 지지 근거 | 배제 근거 | 확인에 쓸 진단 API |
|---|---|---|---|
| 서비스 주소 설정 오류 | 토폴로지 `unresolved`, 로그의 호출 대상이 localhost·잘못된 이름 | 모든 주소 엣지가 `declared` | `get_topology`, `get_pod_logs` |
| Service selector가 Pod를 고르지 못함 | 엔드포인트 0, selector와 Pod 라벨 불일치 | 엔드포인트가 있음 | `get_resource` |
| targetPort 불일치 | 엔드포인트는 있으나 포트가 컨테이너 포트와 다름, Pod 직접 호출은 성공 | Service 포트로 호출해도 성공 | `get_resource`, `probe` |
| 하위 서비스 다운·기동 실패 | 하위 Pod NotReady·재시작, 엔드포인트 not ready | 하위 Pod Ready이고 직접 호출 성공 | `get_resource`, `get_events` |
| 네트워크 정책 차단 | Pod 직접 호출 시간 초과, NetworkPolicy 존재 | 같은 경로 직접 호출 성공 | `probe`, `get_resource` |

- 판정 기준: 서로 다른 출처의 근거 2개 이상으로 지지되고 나머지 후보가 배제되면 확정한다.

## 흔한 함정

- Pod가 전부 Ready여도 이 증상이 난다. readinessProbe가 TCP면 포트만 열려도 Ready다.
- 주소 env가 아예 없으면 코드 기본값(대개 localhost)으로 호출한다. 토폴로지에 엣지가 없다고 정상이 아니다. 로그의 호출 대상을 본다.
- DNS 이름은 Deployment가 아니라 Service 이름을 따른다. 이름이 비슷한 Service가 따로 있을 수 있다.

## 확정하지 못할 때

- 주소가 env가 아니라 코드·설정 파일에 있으면 선언으로 확인할 수 없다 → "확인하지 못함"에 적고 로그 근거로만 판단한다.
- 다른 네임스페이스의 대상은 조회 범위 밖이다.

## 권장 조치 방향

| 원인 후보 | 조치 방향 | 재검증 방법 |
|---|---|---|
| 주소 설정 오류 | 호출하는 쪽 env·ConfigMap의 주소를 실제 Service 이름·포트로 고친다 | 토폴로지 `declared`, 사용자 시나리오 성공 |
| selector 불일치 | Service selector를 Pod 라벨에 맞춘다 | 엔드포인트 수 > 0 |
| targetPort 불일치 | Service `targetPort`를 컨테이너 포트에 맞춘다 | Service 경유 호출 성공 |
| 하위 서비스 다운 | 기동 실패 원인을 failure-after-deploy 지침으로 조사 | 하위 Pod Ready |

## 출처

- Kubernetes [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/)
- 운영 사례: 서비스 주소 env가 없으면 localhost로 호출하는 템플릿 결함 (05-rules S04)
