# W1 학습 노트: 원본 템플릿은 왜 클러스터에서 동작하지 않았나

> 2026-10-19. 템플릿 `shared-infra/kubernetes` 매니페스트를 읽고 정리한 1차본이다. 진단 대상 환경(#7)을 minikube에 띄우면서 실제로 확인한 내용으로 보강한다.

## 이번 주 시나리오

| 시나리오 | 관련 K8s 개념 | 왜 장애가 되나 | 어떤 신호로 보이나 |
|---|---|---|---|
| H0 (정상 기준선) | readinessProbe, Service selector | 장애는 없지만 tcpSocket probe·resources 설정이 권고에 걸린다 | 개선 권고 H01·H02만, 게이트 경로 C |
| A5 (원본 템플릿 상태) | env, Service DNS, envFrom | gateway가 하위 서비스 주소를 env로 받지 못하면 코드 기본값 `localhost`로 호출한다 | Pod는 모두 Ready, 모든 API가 502. 규칙 S04(서비스 주소 설정 오류) |
| A1 (selector 오타) | Service selector ↔ Pod 라벨, EndpointSlice | Service가 Pod를 하나도 고르지 못하면 요청을 받을 곳이 없다 | S01 즉시, Ready 엔드포인트 0 → S02 (15분 뒤) |

## 배운 것

- **Pod가 Ready라고 서비스가 동작하는 것은 아니다.** 템플릿의 readinessProbe는 모두 tcpSocket이라 포트만 열리면 Ready가 된다. 하위 서비스 주소가 틀려 모든 요청이 실패해도 K8s 상태는 정상으로 보인다. 그래서 규칙 점검만으로는 부족하고 사용자 시나리오(증상 감지)가 필요하다.
- **서비스 주소는 선언만 보고도 틀린 것을 알 수 있다.** env의 `*_SERVICE_URL`이 없는 Service나 `localhost`를 가리키면 트래픽이 없어도 적발할 수 있다(S04, 선언 규칙). 주소가 env가 아니라 `envFrom` ConfigMap으로 들어올 수 있어 S04가 envFrom 값도 보게 했다(#37).
- **이름이 하나만 어긋나도 끊긴다.** Service 이름이 Deployment 이름과 다른 경우(`llm-gateway` Deployment ↔ `llm-gateway-svc` Service)가 있고, 라우팅 설정에는 `llm-gateway`로 적혀 있다. DNS 이름은 Service 이름을 따르므로 설정이 실제로 쓰이면 호출이 실패한다.
- **원본 매니페스트는 클라우드 의존을 전제로 한다.** DB·Redis는 Azure 관리형 서비스, 이미지는 사내 레지스트리, 외부 노출은 Azure 내부 LoadBalancer다. 로컬 클러스터에서는 overlay(F6)로 바꿔야 정상 기준선을 만들 수 있다.
- **공유 설정은 장애 범위를 넓힌다.** 네 백엔드가 같은 ConfigMap·Secret을 envFrom으로 받으므로, 키 하나가 빠지면(C01) 여러 서비스가 동시에 시작하지 못한다. 진단할 때 "여러 서비스가 함께 실패" → 공유 설정을 먼저 본다.

## 막혔던 것과 해결

- 진단 대상 환경이 아직 없어(#7) 실제로 띄워 보지 못했다. 매니페스트를 정적으로 읽어 원인을 정리했고, 환경 설명 초안(`app/service/knowledge/environment.md`)에 같은 내용을 반영했다.

## 다음 주에 확인할 것

- gateway가 하위 서비스 주소를 어떤 env 이름으로 받는지, 기본값이 정말 `localhost`인지 (gateway 코드)
- `gateway-dynamic-conf`의 `llm-gateway` 주소를 gateway가 실제로 쓰는지
- llm-gateway의 상태·할당량 API 경로 (`kubeguardian.yaml`의 `app_apis`)
- 로그인 → 채팅 API 경로와 테스트 계정 (F5, `api_scenarios`)
