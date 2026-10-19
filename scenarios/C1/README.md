# C1 — llm-gateway 채팅 호출 429 (할당량 초과)

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `KG_CHAOS_NS`(Chaos 객체를 만들 네임스페이스, 기본 `chaos-mesh`), `TEST_ENV`(테스트 사용자 파일, 기본 `.env.test-user`), `TARGET_MODEL`(기본 `gpt-4.1`).
전제: `make seed`.

## 주입 방식

기획서 §8.2의 원래 주입은 "할당량 하향"이었지만, 템플릿 llm-gateway는 할당량을 **저장만 하고 강제하지 않는다.** 할당량을 0이나 1로 내려도 채팅은 계속 성공한다. 포크 코드는 바꾸지 않기로 했으므로(진단 대상 수정은 F 목록으로 관리) 2026-10-20 사용자 결정으로 (b) HTTPChaos 방식을 택했다. 기획서 §8.2 C1 행도 같이 고쳤다.

Chaos Mesh HTTPChaos(`mode: all`, `target: Response`, `port: 8080`, `method: POST`, `path: /api/v1/chat/completions`)로 llm-gateway Pod의 채팅 완성 응답을 아래로 바꾼다. 객체는 `chaos-mesh/HTTPChaos/kg-http-2`로, 진단 대상 네임스페이스 밖에 만든다. 이름에는 시나리오 ID를 넣지 않는다.

- 응답 코드 429, `Content-Type: application/json`
- 본문은 OpenAI API의 할당량 초과 오류와 같은 모양: `{"error":{"message":"You exceeded your current quota, ...","type":"insufficient_quota","param":null,"code":"insufficient_quota"}}` (HTTPChaos `replace.body`는 base64)
- 다른 경로(할당량·관리 API 등)와 GET 요청은 그대로다. readinessProbe는 tcpSocket이라 Pod는 Ready로 남는다.
- Chaos Mesh는 `dev`에 PodHttpChaos 객체를 만든다. HTTPChaos를 지워도 spec이 빈 채로 남으므로(Chaos Mesh 동작), `reset.sh`가 `dev`의 PodNetworkChaos·PodHttpChaos를 모두 지운다. 주입 중에는 남아 있다 (KubeGuardian은 이 kind를 수집하지 않는다).
- 주입·원복 모두 Pod를 재시작하지 않는다.

### 이 방식의 한계

원인 문구는 "llm-gateway가 LLM 호출에 429(할당량 초과)를 돌려줌"까지다. llm-gateway의 할당량 API(`/api/v1/quotas/{org_id}/usage`) 값은 정상(사용량은 늘 `null`)이므로, "조직 할당량이 소진됨"이라는 원인은 근거로 확인할 수 없다. 에이전트가 볼 수 있는 근거는 agent 로그의 `LLM Gateway error: 429`와 `Error detail: {... "insufficient_quota" ...}`, llm-gateway 직접 호출의 429다.

## 기대 증상

- llm-gateway·agent·gateway·front-chat Pod는 모두 Ready, 재시작 없음.
- 로그인은 성공한다.
- 채팅(`POST /agent/invoke`)은 HTTP 200인데 answer가 `오류가 발생했습니다: LLM Gateway error: 429`다. 템플릿 agent는 LLM 호출이 실패해도 200에 오류 문구를 answer로 돌려준다 (`backend-agent/app/common/util/llm_gateway_client.py` `_post_non_streaming`). 기획서의 "gateway 502"는 실제로는 "200 + 오류 문구"로 보인다.
- agent 로그: `LLM Gateway error: 429`, `Error detail: {"error":{... "type":"insufficient_quota" ...}}`.
- agent Pod에서 llm-gateway `/api/v1/chat/completions`를 직접 부르면 429 (정상일 때는 인증 헤더가 없어 401).
- 규칙 점검: 장애 신호 없음, 경로 C. 기대 경로 B는 증상 감지(API 시나리오)가 생긴 뒤에 나온다.

## 확인 상태

2026-10-20 템플릿 포크(`dev`)에서 확인했다.

- 주입 전 verify_symptom 실패 (채팅 정상 응답 = 증상 없음).
- inject → AllInjected → verify_symptom 통과 (`채팅 answer '오류가 발생했습니다: LLM Gateway error: 429'`, 직접 호출 429, 응답 본문·Content-Type 교체 확인). llm-gateway restartCount 0.
- reset → HTTPChaos·PodHttpChaos 삭제(`kubectl get` 결과 없음), verify_symptom 실패(채팅 정상), 직접 호출 401, H0 스모크 성공.
- `make eval S=C1`: 아직 실행하지 않았다. 기대 결과는 경로 C(증상 감지가 아직 없음), 원복·H0 복구 성공.

## 참고: 할당량 강제가 없는 근거

2026-10-19, `~/Cursor/kg-target/backend-llm-gateway` 코드 확인.

| 근거 | 내용 |
|---|---|
| `app/service/chat_completion.py` 43~93행 `prepare_chat_completion` | 채팅 전 처리는 "LLM 라우터 인가 → 프롬프트 주입 → Provider 결정"뿐이다. 할당량을 읽지 않는다 |
| `app/service/llm_router.py` 23~36행 `check` | (조직, provider, model) 할당 여부만 보고 ALLOW/DENY(403)를 돌려준다 |
| `app/service/quota.py` 59~67행 `get_quota_usage` | `monthly_used`·`daily_used`·`*_remaining`을 항상 `None`으로 돌려준다. 사용량을 세지 않는다 |
| `app/api/routes/quota.py` 44행 | 주석 "used/remaining은 Redis 미연동으로 null" |

검토한 다른 안: (a) 포크에 할당량 강제 추가(약 40~60줄, F 목록 추가 필요), (c) 조직의 모델 할당 제거 → 403(원인이 할당량이 아니라 설정 누락이 됨).

예시 설정 `kubeguardian.example.yaml`의 `app_apis.llm-gateway`는 `/api/v1/quota`인데 실제 라우트는 `/api/v1/quotas/{org_id}/usage`다 (`app/api/router.py` 22행).
