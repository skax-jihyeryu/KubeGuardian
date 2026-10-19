# 환경 설명 — 템플릿 기반 AI 서비스 (진단 대상)

> 초안 (2026-10-19). 출처: 템플릿 `shared-infra/kubernetes` 매니페스트를 읽어 정리했다. 클러스터에 띄워 확인한 내용이 아니므로, 진단 대상 환경(#7)을 띄운 뒤 실제 값으로 고친다.
> 이 문서는 조사 에이전트의 시스템 프롬프트에 들어간다. 이 환경에만 있는 서비스 이름·주소·구조는 여기에만 적고 조사 지침(runbooks/)에는 쓰지 않는다. 비밀값은 적지 않는다.

## 서비스 구성 (네임스페이스 `dev`)

| 서비스 | Service 이름 | 포트 | 역할 |
|---|---|---|---|
| front-chat | `front-chat-svc` | 80 | 채팅 화면. nginx가 `/api`를 gateway로 넘긴다 |
| front-admin | `front-admin-svc` | 80 | 관리자 화면. nginx가 `/api`를 gateway로 넘긴다 |
| gateway | `gateway` | 8000 | 인증과 API 라우팅. 경로 접두사로 하위 서비스를 고른다 |
| admin | `admin` | 8001 | 인증·사용자·조직·AI 리소스 관리 |
| agent | `agent` | 8006 | RAG·멘토 에이전트 (채팅 응답) |
| llm-gateway | `llm-gateway-svc` | 8080 | LLM 배포·할당량·호출 대행 |
| postgres, redis | (로컬 overlay에서 추가) | 5432, 6379 | 원본은 클러스터 밖 Azure PostgreSQL·Azure Redis를 가리킨다 |

- Service 이름과 Deployment 이름이 다른 곳이 있다: front-chat·front-admin은 `-svc`, llm-gateway는 `llm-gateway-svc`. 라벨은 모두 `app: <Deployment 이름>`.
- 외부 노출용 `*-lb` Service(LoadBalancer)가 따로 있다. minikube에서는 외부 IP를 받지 못한다.

## 호출 관계

```text
front-chat / front-admin ──/api──▶ gateway ─┬─ /api/v1/auth/*        → gateway 자신
                                             ├─ /api/v1/admin/*       → admin
                                             ├─ /api/v1/agent/*       → agent ──▶ llm-gateway
                                             └─ /api/v1/llm-gateway/* → llm-gateway
admin, agent, llm-gateway ──▶ postgres · redis
llm-gateway ──▶ Azure OpenAI (서비스용, 클러스터 밖)
```

- gateway는 경로의 접두사를 떼고(`/api/v1/agent/x` → `/api/v1/x`) 하위 서비스로 넘긴다. 실패하면 2번 재시도한다.
- gateway가 하위 서비스 주소를 무엇으로 정하는지가 중요하다. 매니페스트 env에는 서비스 주소 설정(`*_SERVICE_URL`)이 없고, 코드 기본값은 `localhost`다 (기획서 §2.3 T1). 주소 설정이 빠지면 Pod는 Ready인데 모든 API가 502가 된다.
- 라우팅 참조용 설정(`gateway-dynamic-conf` ConfigMap)은 llm-gateway를 `http://llm-gateway:8080/`으로 적었지만 실제 Service 이름은 `llm-gateway-svc`다. gateway가 이 설정을 실제로 쓰는지는 #7에서 확인한다.

## 공유 설정

| 객체 | 종류 | 키 | 쓰는 곳 |
|---|---|---|---|
| `common-config` | ConfigMap | `ENVIRONMENT`, `DATA_PATH` | gateway·admin·agent·llm-gateway (`envFrom`) |
| `infra-config` | ConfigMap | `DB_*`(엔진·호스트·포트·이름·사용자·풀), `REDIS_HOST`·`REDIS_PORT`·`REDIS_USE_SSL` | 같음 |
| `common-secret` | Secret | `MASTER_KEY`, `DB_PASSWORD`, `REDIS_ACCESS_KEY` | 같음 |
| `gateway-conf`, `gateway-dynamic-conf` | ConfigMap | gateway 설정·라우팅 참조 | gateway |
| `front-node-server`, `front-admin-node-server` | ConfigMap | nginx 설정 (`proxy_pass http://gateway:8000`) | front-chat·front-admin |

- 네 백엔드가 같은 ConfigMap·Secret을 `envFrom`으로 받는다. 이 객체 하나가 잘못되면 여러 서비스가 함께 실패한다.
- 원본 `infra-config`는 클러스터 밖 DB·Redis(SSL 6380)를 가리킨다. 로컬에서는 overlay(F6)로 클러스터 안 postgres·redis를 가리키게 바꾼다.

## 상태 확인 방법

- 모든 백엔드·프론트의 readinessProbe는 **tcpSocket**이다. 포트만 열려 있으면 앱이 오류를 내도 Ready로 남는다 (개선 권고 H01). Pod가 Ready라고 앱이 정상이라는 뜻이 아니다.
- 앱 상태는 사용자 시나리오(로그인 → 채팅)와 llm-gateway의 상태·할당량 API로 확인한다. 정확한 경로는 #7에서 확인해 `kubeguardian.yaml`의 `app_apis`에 적는다.
- replicas는 원본이 모두 1이다. 일부 Pod만 오류인 시나리오(B1)를 위해 agent는 로컬 overlay에서 3으로 늘린다.

## 그 밖에 알아 둘 것

- 템플릿 ServiceAccount `dev-account`에 `cluster-admin`이 묶여 있다. 진단 대상의 권한 설정이며, KubeGuardian은 자기 ServiceAccount(`kubeguardian`, 조회 전용)를 쓴다.
- 이미지는 사내 레지스트리(`workforce2.azurecr.io`)를 가리킨다. 로컬에서는 직접 빌드해 `minikube image load`로 넣는다.
- 데이터 볼륨은 `emptyDir`라 Pod가 다시 뜨면 비워진다.
