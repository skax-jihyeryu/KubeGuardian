# 환경 설명 — 템플릿 기반 AI 서비스 (진단 대상)

> 2026-10-19 진단 대상 환경(#7)을 minikube `dev`에 띄워 확인한 내용이다. 출처: 템플릿 매니페스트와 각 서비스 코드, 실제 호출 결과.
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
| postgres, redis | `postgres`, `redis` | 5432, 6379 | 클러스터 안 DB·캐시 (원본 템플릿은 클러스터 밖 관리형 서비스를 가리킨다) |

- Service 이름과 Deployment 이름이 다른 곳이 있다: front-chat·front-admin은 `-svc`, llm-gateway는 `llm-gateway-svc`. 라벨은 모두 `app: <Deployment 이름>`.
- agent는 replicas 3, 나머지는 1이다. 외부 노출용 LoadBalancer Service는 없고 port-forward로 접속한다.

## 호출 관계

```text
front-chat / front-admin ──/api──▶ gateway ─┬─ /api/v1/auth/*        → admin
                                             ├─ /api/v1/admin/*       → admin
                                             ├─ /api/v1/agent/*       → agent ──▶ llm-gateway
                                             └─ /api/v1/llm-gateway/* → llm-gateway
admin, agent, llm-gateway ──▶ postgres · redis
llm-gateway ──▶ Azure OpenAI (서비스용, 클러스터 밖)
```

- gateway는 `/api/v1/auth/*`를 그대로 admin에 넘기고, 나머지는 접두사를 떼고(`/api/v1/agent/x` → `/api/v1/x`) 하위 서비스로 넘긴다. 실패하면 2번 재시도한다. 라우팅 표는 코드에 고정되어 있고 `gateway-dynamic-conf` ConfigMap은 쓰이지 않는다.
- gateway는 요청마다 admin `/api/v1/auth/verify`로 토큰을 검증한 뒤 사용자·조직 정보를 헤더로 붙여 넘긴다.
- 서비스 주소는 env로 받는다. env가 없으면 코드 기본값 `localhost`로 호출한다.

| 서비스 | 주소 env | 가리켜야 할 곳 |
|---|---|---|
| gateway | `ADMIN_SERVICE_URL`, `AUTH_VERIFY_URL` | admin:8001 |
| gateway | `AGENT_SERVICE_URL` | agent:8006 |
| gateway | `LLM_GATEWAY_SERVICE_URL` | llm-gateway-svc:8080 |
| admin | `LLM_GATEWAY_BASE_URL` | llm-gateway-svc:8080 |
| agent | `BASE_SERVICE_URI` (admin), `LLM_GATEWAY_URL` | admin:8001, llm-gateway-svc:8080 |

## 공유 설정

| 객체 | 종류 | 키 | 쓰는 곳 |
|---|---|---|---|
| `common-config` | ConfigMap | `ENVIRONMENT`, `DATA_PATH` | gateway·admin·agent·llm-gateway (`envFrom`) |
| `infra-config` | ConfigMap | `DB_*`(엔진·호스트·포트·이름·사용자·풀), `REDIS_HOST`·`REDIS_PORT`·`REDIS_USE_SSL` | 같음 |
| `common-secret` | Secret | `MASTER_KEY`, `DB_PASSWORD`, `REDIS_ACCESS_KEY` | 같음 |
| `gateway-conf`, `gateway-dynamic-conf` | ConfigMap | gateway 설정·라우팅 참조 | gateway |
| `front-node-server`, `front-admin-node-server` | ConfigMap | nginx 설정 (`proxy_pass http://gateway:8000`) | front-chat·front-admin |

- 네 백엔드가 같은 ConfigMap·Secret을 `envFrom`으로 받는다. 이 객체 하나가 잘못되면 여러 서비스가 함께 실패한다.
- `infra-config`는 클러스터 안 `postgres`·`redis`(SSL 없음, 6379)를 가리킨다. admin·llm-gateway는 SSL 없는 redis 주소를 만들 때 포트 설정을 무시하므로 6379에서만 동작한다.

## 상태 확인 방법

- 모든 백엔드·프론트의 readinessProbe는 **tcpSocket**이다. 포트만 열려 있으면 앱이 오류를 내도 Ready로 남는다 (개선 권고 H01). Pod가 Ready라고 앱이 정상이라는 뜻이 아니다.
- 인증 없이 부를 수 있는 상태 확인 API는 없다. gateway `/health`는 다른 라우트에 가려 404다.
- 앱 상태는 사용자 시나리오로 확인한다: 로그인 `POST /api/v1/auth/login` (`{"id","password"}` → `{"access_token","user"}`), 채팅 `POST /api/v1/agent/invoke` (`{"query","model","org_id"}` → `{"success","data":{"answer"}}`).
- 채팅이 실패해도 HTTP 200에 answer가 `오류가 발생했습니다: LLM Gateway error: <코드>`로 올 수 있다. 상태 코드만 보지 말고 answer 문구를 본다 (403 = 조직에 그 모델이 할당되지 않음, 500 = llm-gateway 내부·외부 호출 실패).
- llm-gateway: LLM 배포(엔드포인트·키·모델)는 DB에 저장되고 조직별로 할당한다. 상태 확인 `POST /api/v1/health`(내부 키 필요, 실제 LLM 호출), 할당량 조회 `GET /api/v1/quotas/{org_id}/usage`(관리자).

## 그 밖에 알아 둘 것

- 템플릿 ServiceAccount `dev-account`에 `cluster-admin`이 묶여 있다. 진단 대상의 권한 설정이며, KubeGuardian은 자기 ServiceAccount(`kubeguardian`, 조회 전용)를 쓴다.
- 이미지는 노드에서 직접 빌드한 `kg-target/<서비스>:local`이다 (`imagePullPolicy: Never`).
- llm-gateway는 사내 SSL 프록시를 거쳐 외부 LLM API를 부르므로 사내 CA 번들(`SSL_CERT_FILE`)을 마운트한다. 이 번들이 없으면 외부 호출이 인증서 오류로 실패한다.
- 앱은 DB 연결을 재시도하지 않는다. postgres보다 먼저 뜨면 기동 직후 1회 재시작될 수 있다.
- 데이터 볼륨은 `emptyDir`라 Pod가 다시 뜨면 비워진다.
