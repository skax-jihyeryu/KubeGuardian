# KubeGuardian AI — 규칙 점검 명세 (감지기)

> 기준: [기획서](01-project-proposal.md) §7, [상세 설계](산출물/04-상세설계및개발환경구축.md) §4 조사 지침 목록
> 작성일: 2026-10-14
>
> 규칙 점검은 LLM을 쓰지 않는 결정적 코드다. 이 문서는 감지기마다 **무엇을, 언제, 어떤 근거로 적발하는지**를 정한다. 감지기 구현과 단위 테스트는 이 문서를 기준으로 한다.

## 0. 원칙

| 원칙 | 내용 |
|---|---|
| 결정적 판정 | 한 번의 점검에서 수집한 스냅샷과 직전 관측 기록만으로 판정한다. 같은 입력이면 같은 결과가 나온다 |
| 지속 시간은 kubernetes-mixin을 따른다 | 상태 규칙은 [kubernetes-mixin](https://github.com/kubernetes-monitoring/kubernetes-mixin) 알림 규칙의 `for` 지속 시간을 그대로 쓴다. 롤아웃 중 일시적인 부족처럼 금방 회복되는 상태를 장애로 올리지 않기 위해서다. mixin에 대응 규칙이 없는 선언 규칙은 spec만 보고 즉시 판정한다 |
| 수집 실패는 통과가 아니다 | 필요한 데이터를 못 읽으면 판정 결과를 "확인하지 못함"으로 둔다 |
| 판정에 쓴 값을 남긴다 | 적발·통과 모두 판정에 쓴 값을 근거로 저장한다. 보고서 화면 P4에 "판정에 쓴 값"으로 표시한다 |
| 진단 대상 네임스페이스만 | 대상 네임스페이스 밖의 리소스는 판정에 쓰지 않는다. 노드(N01)는 대상 Pod가 올라간 노드만 본다 |

### 0.1 판정 결과

| 결과 | 뜻 | 게이트 |
|---|---|---|
| 적발 | 조건을 만족하고 지속 시간도 채움 | 장애 신호면 경로 A를 연다 |
| 관찰 중 | 조건은 만족했지만 지속 시간이 아직 안 됨 | 열지 않는다. P4에 "관찰 중"으로 표시 |
| 통과 | 조건을 만족하지 않음 | — |
| 확인하지 못함 | 수집 실패 | 열지 않는다. P4와 보고서의 "확인하지 못함"에 표시 |

### 0.2 지속 시간 판정 방법

mixin은 Prometheus가 15초~1분 간격으로 평가한 결과가 `for` 동안 계속 참이어야 알린다. 우리는 5분마다 점검하므로 관측 기록으로 같은 효과를 낸다.

- `core/db`의 관측 기록: (규칙 ID, 대상 리소스, 최초 관측 시각, 마지막 관측 시각)
- 조건을 만족하면 기록이 없을 때 최초 관측 시각을 지금으로 저장하고, 있으면 마지막 관측 시각만 갱신한다.
- `지금 - 최초 관측 시각 ≥ for`이면 적발한다. 5분 주기에서 `for: 15m`이면 최초 관측 후 네 번째 점검(15분 경과)에 적발된다.
- 조건을 만족하지 않으면 기록을 지운다 (연속성이 끊김).
- "확인하지 못함"이면 기록을 그대로 둔다 (판정 보류).
- 점검 간격 사이에 잠깐 회복됐다 다시 나빠진 경우는 놓칠 수 있다. mixin이 `max_over_time`으로 보정하는 부분은 규칙별로 적었다 (P06).

### 0.3 묶음용 데이터를 수집하지 못했을 때

Service·ReplicaSet은 여러 규칙에서 판정 조건이 아니라 **신호를 서비스로 묶는 데만** 쓴다. 이것을 못 읽었다고 Pod 규칙까지 "확인하지 못함"으로 두면 장애를 놓치므로, 묶음만 바꿔 판정을 계속한다.

| 항목 | 내용 |
|---|---|
| 대상 규칙 | Service 실패: P03, P06, P07, C01, H02, W01, W02. ReplicaSet 실패: P03, P06, P07, C01, H02, S02(롤아웃 제외만 생략) |
| 묶음 | Service를 못 읽으면 서비스 이름 대신 Deployment 이름으로 묶는다 (Service가 고르지 않는 Pod를 묶는 방식과 같다). ReplicaSet을 못 읽으면 Pod의 ReplicaSet 소유자 이름(`<Deployment>-<pod-template-hash>`)에서 끝의 해시를 떼어 Deployment 이름으로 쓴다 |
| 1차 심각도 | Service를 못 읽은 상태에서 나온 적발은 🔴 긴급을 🟠 주의로 낮춘다. 서비스 이름이 핵심 경로(§3)와 맞는지 확신할 수 없기 때문이다. ReplicaSet만 못 읽었으면 서비스 이름은 Service selector로 정해지므로 낮추지 않는다 |
| 근거로 남길 값 | 각 신호의 판정에 쓴 값에 `failed_kinds`(못 읽은 kind → 사유)를 넣는다. 묶음을 바꿔 판정했다는 사실이 신호마다 남는다 |
| 계속 "확인하지 못함" | 판정 조건에 직접 쓰는 kind가 실패한 경우. H01(Service가 고르는 Pod만 봄), W02의 ReplicaSet(새·이전 ReplicaSet의 Ready 수), S 규칙의 Service, Pod·Deployment·ConfigMap·Secret·EndpointSlice·Node |

## 1. ID 체계

ID는 **대상 리소스 카테고리 접두사 + 번호**다.

| 접두사 | 뜻 | 대상 |
|---|---|---|
| N | Node | 노드 상태 |
| P | Pod | 컨테이너 대기·재시작·종료 |
| W | Workload | Deployment·ReplicaSet |
| S | Service | Service, EndpointSlice, 포트, 서비스 주소 |
| C | Config | ConfigMap·Secret 참조 |
| H | 개선 권고 (Hygiene) | probe, resources 같은 설정 모범 사례 |

- **장애 신호**(게이트를 연다): N·P·W·S·C
- **개선 권고**(게이트를 열지 않음, 알림 없음): H
- 번호는 다시 쓰지 않는다. 확장 후보(기획서 §7.2)의 번호도 예약된 것으로 본다.

## 2. 규칙 요약

| ID | 이름 | 성격 | 판정 시점 | 1차 심각도 | 근거 | 관련 지침 |
|---|---|---|---|---|---|---|
| N01 | 노드 NotReady | 상태 | 15분 지속 | 🔴 긴급 | mixin `KubeNodeNotReady` | partial-failure |
| P03 | 이미지 받기 실패 | 상태 | 1시간 지속 | 🟠 주의 / 🔴 긴급 | mixin `KubeContainerWaiting`, K8s Debug Pods | failure-after-deploy |
| P06 | 반복 재시작 (CrashLoop) | 시간 | 15분 지속 | 🟠 주의 / 🔴 긴급 | mixin `KubePodCrashLooping` | failure-after-deploy, noise-filtering |
| P07 | OOMKilled | 상태 | 즉시 (최근 15분 안 종료) | 🟠 주의 | 설계값 (mixin 대응 규칙 없음) | partial-failure, noise-filtering |
| W01 | 가용 replicas 부족 | 상태 | 15분 지속 | 🟠 주의 / 🔴 긴급 | mixin `KubeDeploymentReplicasMismatch` | failure-after-deploy |
| **W02** | **롤아웃 정체** (확장 후보에서 승격) | 상태 | 15분 지속 | 🟠 주의 / 🔴 긴급 | mixin `KubeDeploymentRolloutStuck`, K8s Deployment | failure-after-deploy |
| S01 | selector에 맞는 Pod 없음 | 선언 | 즉시 | 🔴 긴급 / 🟠 주의 | K8s Debug Services | service-unreachable |
| S02 | Ready 엔드포인트 없음·일부 | 상태 | 15분 지속 | 🔴 긴급 / 🟠 주의 | mixin `KubePodNotReady`, K8s EndpointSlice | service-unreachable, partial-failure |
| S03 | targetPort 불일치 | 선언 | 즉시 | 🔴 긴급 / 🟠 주의 | K8s Debug Services | service-unreachable |
| S04 | 서비스 주소 설정 오류 | 선언 | 즉시 | 🔴 긴급 / 🟠 주의 | 운영 사례 (템플릿 결함 T1) | service-unreachable |
| C01 | 설정 참조 누락 | 선언 | 즉시 | 🔴 긴급 / 🟠 주의 | K8s ConfigMap 사용 문서 | failure-after-deploy |
| H01 | readinessProbe 없음·TCP만 | 선언 | 즉시 | ⚪ 참고 | Polaris `readinessProbeMissing`, K8s Probes | partial-failure |
| H02 | requests·limits 미설정 | 선언 | 즉시 | ⚪ 참고 | Polaris (항목 미확인) | — |

- 근거 열의 "설계값"·"미확인"은 외부 기준으로 확인하지 못한 항목이다. W4 평가 결과로 조정한다 (§6).
- W02는 v2.3의 확장 후보였던 규칙(같은 판정 조건)을 MVP 범위로 올린 것이다. 이유는 §5.1.

## 3. 1차 심각도

1차 감지 알림은 AI 조사 전에 나가므로 심각도도 규칙 결과만으로 정한다. AI 조사가 끝나면 보고서의 영향 판단(`impact`)으로 갱신한다.

**핵심 경로**는 설정 파일 `kubeguardian.yaml`의 `core_paths`에 서비스 목록으로 적는다. 사용자 기능 단위로 적는다 (예: 로그인 경로, 채팅 경로에 속한 서비스). 서비스 이름은 환경 설정에만 두고 이 문서와 지침에는 쓰지 않는다.

| 심각도 | 조건 |
|---|---|
| 🔴 긴급 | 적발된 대상 서비스가 핵심 경로에 있고, 그 서비스가 요청을 하나도 받을 수 없음 (가용 Pod 0, Ready 엔드포인트 0, 연결할 Service가 없음, 노드 장애) |
| 🟠 주의 | 일부만 가용함, 핵심 경로가 아닌 서비스, 이전 버전 Pod가 아직 요청을 받는 롤아웃 실패 |
| ⚪ 참고 | 개선 권고 (H) |

- 한 점검에서 여러 규칙이 적발되면 가장 높은 심각도를 쓴다.
- 경로 B(증상만 있음)는 실패한 API 시나리오 단계가 핵심 경로 기능이면 긴급, 아니면 주의다.

## 4. 규칙 상세

각 규칙은 같은 항목으로 적는다: 판정 조건, 지속 시간, 제외 조건, 1차 심각도, 근거로 남길 값, 출처.

### N01 — 노드 NotReady

| 항목 | 내용 |
|---|---|
| 판정 조건 | 대상 Pod가 올라간 노드의 `status.conditions[type=Ready].status` ≠ `True` |
| 지속 시간 | 15분 |
| 제외 | `spec.unschedulable = true`인 노드 (운영자가 일부러 cordon한 노드) |
| 1차 심각도 | 🔴 긴급 (단일 노드 환경이라 전체 서비스 영향) |
| 근거로 남길 값 | 노드 이름, Ready 상태·reason·message, 마지막 전이 시각 |
| 출처 | mixin `KubeNodeNotReady`: Ready가 true가 아니고 unschedulable이 아님, `for: 15m`. [Runbook](https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubenodenotready/) |

### P03 — 이미지 받기 실패

| 항목 | 내용 |
|---|---|
| 판정 조건 | 컨테이너 `state.waiting.reason` ∈ {`ErrImagePull`, `ImagePullBackOff`, `ErrImageNeverPull`, `InvalidImageName`}. `ErrImageNeverPull`은 `imagePullPolicy: Never`인데 노드에 이미지가 없는 경우로, 이미지를 받지 못한다는 의미가 같다 |
| 지속 시간 | 1시간 |
| 제외 | — |
| 1차 심각도 | 같은 Deployment에 Ready Pod가 남아 있으면(이전 버전이 서비스 중) 🟠 주의, 없으면 §3 기준 |
| 근거로 남길 값 | Pod, 컨테이너, 이미지 이름, 대기 사유·메시지, 소유 ReplicaSet, 같은 Deployment의 Ready Pod 수 |
| 출처 | mixin `KubeContainerWaiting`: CrashLoopBackOff가 아닌 대기 사유, `for: 1h`. K8s [Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/): Waiting 상태의 가장 흔한 원인은 이미지 받기 실패 |

### P06 — 반복 재시작 (CrashLoop)

| 항목 | 내용 |
|---|---|
| 판정 조건 | 직전 점검 이후 다음 중 하나가 관측됨: 컨테이너 `state.waiting.reason = CrashLoopBackOff`, 또는 `restartCount`가 직전 점검 값보다 증가 |
| 지속 시간 | 15분 |
| 제외 | 지금 Ready이고 직전 점검 이후 `restartCount`가 늘지 않은 컨테이너. 이미 회복된 과거 재시작은 장애 신호가 아니다 |
| 직전 스냅샷 없음 | 직전 점검 스냅샷이 없거나(첫 점검) 직전에 없던 컨테이너는 `restartCount`를 비교하지 않고 `CrashLoopBackOff` 상태만으로 판정한다. 첫 점검에서 과거 재시작을 증가로 오인하지 않기 위함이다 |
| 1차 심각도 | §3 기준 (가용 Pod 수로 판단) |
| 근거로 남길 값 | Pod, 컨테이너, `restartCount`(현재·직전), 대기 사유, `lastState.terminated`의 reason·exitCode·종료 시각 |
| 출처 | mixin `KubePodCrashLooping`: 5분 구간에 CrashLoopBackOff가 한 번이라도 보이면 참(`max_over_time[5m] >= 1`), `for: 15m`. 우리는 재시작 사이에 컨테이너가 잠깐 Running이 되는 것을 놓치지 않도록 `restartCount` 증가로 보정한다. [Runbook](https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping/)의 원인 목록(리소스 부족, 의존 서비스 미준비, 설정·ConfigMap·Secret 누락 등)은 지침에 쓴다 |

### P07 — OOMKilled

| 항목 | 내용 |
|---|---|
| 판정 조건 | 컨테이너 `lastState.terminated.reason = OOMKilled`이고 종료 시각이 최근 15분 안 |
| 지속 시간 | 없음 (종료 시각으로 판정) |
| 제외 | 종료 시각이 15분보다 오래됨 (이미 지난 일. 반복되면 P06이 잡는다) |
| 1차 심각도 | 🟠 주의 |
| 근거로 남길 값 | Pod, 컨테이너, 종료 시각, memory limits·requests |
| 출처 | **설계값.** kubernetes-mixin 알림 규칙에는 OOMKilled 대응 규칙이 없다. 15분은 다른 규칙의 지속 시간에 맞춘 값이며 W4 평가에서 조정한다 |

### W01 — 가용 replicas 부족

| 항목 | 내용 |
|---|---|
| 판정 조건 | Deployment `spec.replicas` > `status.availableReplicas` **그리고** `status.updatedReplicas`가 최근 10분 동안 바뀌지 않음 |
| 지속 시간 | 15분 |
| 제외 | `updatedReplicas`가 바뀌는 중 (롤아웃이 진행되고 있음) |
| 첫 관측 | replicas 부족을 처음 관측하면 이전 `updatedReplicas` 값을 모르므로 그 시각에 방금 바뀐 것으로 본다. 그래서 10분 뒤에 "바뀌지 않음"이 되고, 그때부터 15분 지속을 재므로 첫 관측 후 약 25분에 적발된다 |
| 1차 심각도 | `availableReplicas = 0`이면 §3 기준, 일부면 🟠 주의 |
| 근거로 남길 값 | replicas, availableReplicas, updatedReplicas와 마지막 변경 시각 |
| 출처 | mixin `KubeDeploymentReplicasMismatch`: 위 두 조건, `for: 15m`. [Runbook](https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubedeploymentreplicasmismatch/) |

`updatedReplicas`의 변경 시각은 관측 기록에 같이 저장한다.

### W02 — 롤아웃 정체 (확장 후보에서 승격)

| 항목 | 내용 |
|---|---|
| 판정 조건 | Deployment `status.conditions[type=Progressing].status = False` (reason `ProgressDeadlineExceeded`) |
| 지속 시간 | 15분 |
| 제외 | — |
| 1차 심각도 | 이전 버전 Pod가 Ready로 남아 있으면 🟠 주의, 없으면 §3 기준 |
| 근거로 남길 값 | Progressing 조건의 reason·message·전이 시각, `progressDeadlineSeconds`, 새·이전 ReplicaSet의 Ready 수 |
| 출처 | mixin `KubeDeploymentRolloutStuck`: Progressing=false, `for: 15m`. K8s [Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/): `progressDeadlineSeconds` 기본 600초, 넘으면 `Progressing=False`, `reason: ProgressDeadlineExceeded`. Kubernetes는 상태 조건만 남기고 다른 조치를 하지 않는다 |

### S01 — selector에 맞는 Pod 없음

| 항목 | 내용 |
|---|---|
| 판정 조건 | Service `spec.selector`에 맞는 Pod가 네임스페이스에 0개 |
| 지속 시간 | 없음 (선언 비교) |
| 제외 | selector가 없는 Service (ExternalName, 수동 EndpointSlice) |
| 1차 심각도 | §3 기준 (대상 서비스가 요청을 받을 수 없음) |
| 근거로 남길 값 | Service selector, 같은 앱으로 보이는 Pod의 라벨 (비교용) |
| 출처 | K8s [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/): selector가 Pod 라벨과 맞는지 확인. [learnk8s](https://learnkube.com/troubleshooting-deployments): Endpoints가 비면 selector와 Pod 라벨 불일치 |

### S02 — Ready 엔드포인트 없음·일부

| 항목 | 내용 |
|---|---|
| 판정 조건 | Service의 EndpointSlice에서 `conditions.ready = true`인 엔드포인트가 0개(없음) 또는 전체보다 적음(일부) |
| 지속 시간 | 15분 |
| 제외 | selector가 없는 Service, `terminating = true`인 엔드포인트(종료 중), 대상 Pod가 0개 (S01이 다룸), 롤아웃 중인 새 버전 Pod (아래) |
| 롤아웃 중인 새 버전 Pod | Ready가 아닌 엔드포인트 중 다음을 모두 만족하면 전체에서 뺀다. ① 그 Pod의 소유 ReplicaSet이 같은 Deployment의 ReplicaSet 가운데 `deployment.kubernetes.io/revision`이 가장 크다 (새 버전). ② 같은 Service에 같은 Deployment의 이전 revision ReplicaSet Pod가 Ready 엔드포인트로 남아 있다 (이전 버전이 트래픽을 받는 중). 사용자 영향이 없고, 새 버전이 뜨지 않는 원인은 P03·W02가 다룬다. 이전 버전 Pod가 모두 Ready가 아니면 빼지 않는다. 롤아웃이 끝나면 이전 ReplicaSet은 0으로 줄어 ②가 성립하지 않으므로 Deployment 상태(`updatedReplicas`·`observedGeneration`)는 보지 않는다 (replicas 1에서는 `maxUnavailable`이 0으로 내림돼 롤아웃 중에도 `updatedReplicas = replicas`다). ReplicaSet을 수집하지 못했으면 새 버전인지 알 수 없으므로 이 제외 없이 판정하고, 근거 값에 `failed_kinds`를 남긴다 (§0.3). 그 Deployment의 ReplicaSet 중 `deployment.kubernetes.io/revision` 값이 없거나 숫자가 아닌 것이 있으면 새 버전을 정할 수 없으므로 그 Deployment에는 이 제외를 적용하지 않는다. 알려진 한계: Deployment가 일시 중지(`paused`)되면 Progressing이 Unknown이라 W02가 나오지 않으므로, 일시 중지된 롤아웃의 새 버전 Pod는 S02·W02 어느 쪽에도 잡히지 않는다 (P03은 이미지 받기 실패일 때만 잡는다) |
| 1차 심각도 | 없음은 §3 기준, 일부는 🟠 주의 |
| 근거로 남길 값 | Ready / 전체 엔드포인트 수, Ready가 아닌 Pod와 그 Pod의 Ready 조건, 롤아웃 중이라 뺀 새 버전 Pod |
| 출처 | mixin `KubePodNotReady`: Running인데 Ready가 아닌 Pod, `for: 15m`. K8s [EndpointSlice](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/): `ready`는 "serving이고 terminating이 아님". K8s [Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/): Ready가 아닌 Pod는 Service 트래픽을 받지 않음 |

### S03 — targetPort 불일치

| 항목 | 내용 |
|---|---|
| 판정 조건 | Service `ports[].targetPort`가 대상 Pod 컨테이너의 `ports[]`에 없음. 이름으로 지정한 포트는 컨테이너 포트 이름으로 찾는다 |
| 지속 시간 | 없음 (선언 비교) |
| 제외 | 대상 Pod가 0개 (S01이 다룸). 컨테이너에 `ports`를 아예 선언하지 않고 숫자 targetPort를 쓰는 경우는 판정하지 않고 "확인하지 못함"으로 둔다 (컨테이너 포트 선언은 선택 사항이라 오탐이 나기 쉬움) |
| 1차 심각도 | §3 기준 |
| 근거로 남길 값 | Service port·targetPort, 컨테이너 포트 목록 |
| 출처 | K8s [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/): port와 targetPort가 컨테이너가 실제로 듣는 포트를 가리키는지 확인. [learnk8s](https://learnkube.com/troubleshooting-deployments): Endpoints가 있는데 응답이 없으면 targetPort 확인 |

### S04 — 서비스 주소 설정 오류

| 항목 | 내용 |
|---|---|
| 판정 조건 | 컨테이너 env와 `envFrom`으로 불러오는 ConfigMap 값 중 이름이 `*_SERVICE_URL`·`*_URL`인 값의 호스트가 다음 중 하나: `localhost`·`127.0.0.1`, 대상 네임스페이스에 없는 Service 이름, Service에 없는 포트. 우선순위는 Kubernetes 규칙대로 뒤의 envFrom이 앞을 덮고 env가 envFrom을 덮는다. Secret envFrom과 env `valueFrom`은 값을 읽지 않고 판정하지 않으며, 같은 이름의 앞선 값도 판정에서 뺀다. envFrom이 가리키는 ConfigMap·Secret을 수집하지 못했으면 그 서비스의 envFrom 값은 "확인하지 못함"이다. 같은 서비스에 적발된 주소가 하나라도 있으면 적발이 우선한다 |
| 지속 시간 | 없음 (선언 비교) |
| 제외 | 설정 파일 `external_hosts` 허용 목록에 있는 외부 호스트 (예: LLM API 엔드포인트) |
| 1차 심각도 | 그 env를 쓰는 서비스가 핵심 경로면 🔴 긴급, 아니면 🟠 주의 |
| 근거로 남길 값 | 서비스, env 이름, 값(마스킹 후), 매칭 결과(declared / unresolved) |
| 출처 | **운영 사례.** 템플릿에서 서비스 주소 env가 없으면 `localhost`로 호출하는 결함(기획서 §2.3 T1)에서 나온 규칙이다. 외부 표준은 없다 |

### C01 — 설정 참조 누락

| 항목 | 내용 |
|---|---|
| 판정 조건 | Pod spec의 `envFrom`, `env[].valueFrom`(configMapKeyRef·secretKeyRef), `volumes`(configMap·secret)가 참조하는 객체나 키가 없음 |
| 지속 시간 | 없음 (선언 비교) |
| 제외 | `optional: true`인 참조 |
| 1차 심각도 | 참조하는 Pod가 시작되지 못하므로 §3 기준. 이전 버전 Pod가 Ready로 남아 있으면 🟠 주의 |
| 근거로 남길 값 | 참조 위치, 객체 종류·이름·키, 존재 여부 (Secret은 메타데이터와 키 이름만, 값은 읽지 않음) |
| 출처 | K8s [Configure a Pod to Use a ConfigMap](https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/): 없는 ConfigMap이나 키를 참조하고 optional로 표시하지 않으면 Pod가 시작되지 않는다. Secret에도 같은 규칙을 적용하며, Secret 쪽 공식 문서 문구는 아직 확인하지 않았다 |

### H01 — readinessProbe 없음·TCP만

| 항목 | 내용 |
|---|---|
| 판정 조건 | 서비스를 받는 컨테이너(Service가 선택하는 Pod)에 `readinessProbe`가 없거나 `tcpSocket`만 씀 |
| 1차 심각도 | ⚪ 참고 (게이트를 열지 않음, 알림 없음) |
| 근거로 남길 값 | 컨테이너, probe 종류와 설정 |
| 출처 | Polaris `readinessProbeMissing` (warning). K8s [Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/): TCP 검사는 소켓 연결만 확인하므로, 앱이 오류를 내도 Ready로 남을 수 있다 (partial-failure 지침의 함정으로 연결) |

### H02 — requests·limits 미설정

| 항목 | 내용 |
|---|---|
| 판정 조건 | 컨테이너 `resources.requests` 또는 `resources.limits`의 cpu·memory 중 하나라도 없음 |
| 1차 심각도 | ⚪ 참고 |
| 근거로 남길 값 | 컨테이너, 설정된 resources |
| 출처 | Polaris 리소스 검사 항목 (**해당 페이지 미확인**). 확인 후 출처를 보강한다 |

## 5. 판정 시점이 시나리오에 주는 영향

mixin 지속 시간을 따르면 상태 규칙은 장애 후 최소 15분이 지나야 적발된다.

| 규칙 | 장애 주입 후 적발까지 (5분 주기 기준) |
|---|---|
| 선언 규칙 (S01, S03, S04, C01, H01, H02) | 다음 점검 = 0~6분 |
| 15분 규칙 (N01, P06, S02) | 약 15~21분 |
| W01 | 변화 없음 10분 + 15분 = 약 25~31분 (§4 W01 "첫 관측") |
| W02 | `progressDeadlineSeconds` 600초 + 15분 = 약 25~31분 |
| P03 | 약 1시간~1시간 6분 |

| 시나리오 | 적발 규칙 | 영향 |
|---|---|---|
| A1 (selector 오타) | S01 즉시 (S02는 대상 Pod가 0개라 제외) | 영향 없음 |
| A3 (없는 이미지 태그) | **W02 약 25분**, P03 약 1시간 (S02는 롤아웃 중인 새 버전 Pod라 제외) | 아래 §5.1 |
| A5 (서비스 주소 누락) | S04 즉시 | 영향 없음 |
| E1 (과거 재시작 미끼) | P06은 적발되지 않아야 함 (제외 조건) | 의도대로 |
| C1·C2·C5 | 규칙 없음, 증상 감지(경로 B) | 장애 감지 시간 KPI는 이 시나리오들로 재므로 영향 없음 |

### 5.1 A3과 W02

기획서 §8.2는 A3이 P03·W01로 적발된다고 적었지만, **W01은 A3에서 적발되지 않는다.**
- 롤링 업데이트의 `maxUnavailable`은 내림으로 계산된다 (K8s Deployment 문서). replicas가 1~3이면 0이 된다.
- 그래서 새 Pod가 이미지를 못 받아도 이전 Pod가 계속 남아 `availableReplicas = replicas`가 유지된다.
- 이 경우를 잡는 mixin 규칙이 `KubeDeploymentRolloutStuck`이다. 그래서 확장 후보였던 W02를 MVP 범위로 올렸다.

평가에 주는 영향:
- A3 1회 실행에 적발까지 약 25~31분이 걸린다. 2조건 × 3회면 약 3시간이다.
- 1차 심각도는 이전 버전이 서비스 중이라 🟠 주의다 (사용자 영향 없음).
- 기획서 §8.2의 A3 "AI 진입" 열은 `A (W02·P03)`로 고쳤다.

## 6. 근거가 약한 항목과 조정 계획

| 항목 | 상태 | 조정 시점 |
|---|---|---|
| P07의 15분 | 설계값. mixin에 대응 규칙 없음 | W4 평가에서 E1·확장 시나리오 결과로 조정 |
| H02 출처 | Polaris 리소스 검사 페이지 미확인 | 규칙 구현 전(W1)에 확인 |
| C01의 Secret 문구 | ConfigMap 문서로만 확인 | 규칙 구현 전(W1)에 확인 |
| S04 | 외부 표준 없음, 운영 사례 | 유지. 지침 출처에 "운영 사례"로 표시 |
| 지속 시간 단축 | 평가 시간이 길어지면 평가용으로 지속 시간을 줄이는 설정을 둘지 | W2 평가 러너 작성 시 결정 |

## 7. 테스트 기준

- 규칙마다 Kubernetes API 응답을 JSON 픽스처로 저장하고 세 종류를 둔다: **적발**, **정상**, **제외(오탐 방지)**.
- 지속 시간 규칙은 관측 기록을 함께 주고 경계값을 확인한다: 14분 → 관찰 중, 15분 → 적발, 중간에 조건이 풀림 → 기록 삭제.
- 수집 실패 픽스처로 "확인하지 못함"이 나오는지 확인한다.
- 시나리오 inject 후 실제 클러스터 응답을 픽스처로 저장해 회귀 테스트에 추가한다 (H0, A1, A3, A5부터).
- 구현 위치: `app/service/checks/detectors/<규칙 ID 소문자>.py`, 관측 기록은 `app/core/db/`.

## 8. 출처

| 출처 | 쓴 곳 |
|---|---|
| [kubernetes-mixin `apps_alerts.libsonnet`](https://github.com/kubernetes-monitoring/kubernetes-mixin/blob/master/alerts/apps_alerts.libsonnet) | P03, P06, W01, W02, S02의 조건과 지속 시간 |
| [kubernetes-mixin `kubelet.libsonnet`](https://github.com/kubernetes-monitoring/kubernetes-mixin/blob/master/alerts/kubelet.libsonnet) | N01 |
| [Prometheus Operator Runbooks — Kubernetes](https://runbooks.prometheus-operator.dev/runbooks/kubernetes/) | 규칙별 runbook, 지침 원인 목록 |
| [Kubernetes — Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) | S01, S03 |
| [Kubernetes — Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) | P03 |
| [Kubernetes — Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) | W02, §5.1 |
| [Kubernetes — EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/) | S02 |
| [Kubernetes — Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) | S02, H01 |
| [Kubernetes — Configure a Pod to Use a ConfigMap](https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/) | C01 |
| [Polaris — Reliability checks](https://polaris.docs.fairwinds.com/checks/reliability/) | H01 |
| [learnk8s — Troubleshooting Kubernetes deployments](https://learnkube.com/troubleshooting-deployments) | S01, S03 |
| [K8sGPT analyzers](https://github.com/k8sgpt-ai/k8sgpt) | 규칙 범위 비교 (Pod, Service, Deployment, Node, ConfigMap) |
| 기획서 §2.3 템플릿 결함 T1 | S04 |
