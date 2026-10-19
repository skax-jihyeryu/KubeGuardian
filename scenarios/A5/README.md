# A5 — 서비스 주소 설정 누락 (원본 템플릿 상태)

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`).

## 주입 내용

gateway Deployment의 하위 서비스 주소 env 4개(`ADMIN_SERVICE_URL`, `AUTH_VERIFY_URL`, `AGENT_SERVICE_URL`, `LLM_GATEWAY_SERVICE_URL`)를 템플릿 코드 기본값인 `localhost` 주소로 바꾼다. 원래 값은 `scenarios/A5/.state/<네임스페이스>-gateway.env`(커밋하지 않음)에 저장하고 `reset.sh`가 되돌린다.

기획서는 A5를 "F1 제거"(env 자체를 지움)로 적었다. env를 지우면 앱 동작은 같지만(코드 기본값이 같은 `localhost` 주소) S04가 판정할 값이 없다. S04는 값이 `localhost`·없는 Service를 가리키는지 보는 선언 규칙이라, 값을 코드 기본값으로 남기는 방식으로 주입한다.

## 기대 증상

- front-chat 경유 로그인이 500으로 실패한다 (gateway → admin을 `localhost`로 호출). 채팅 등 모든 API가 실패한다.
- gateway Pod는 Ready로 남는다 (readinessProbe가 tcpSocket).
- 규칙 점검: S04가 gateway를 즉시 적발(긴급, 로그인·채팅 핵심 경로), 게이트 경로 A.

## 확인 상태

2026-10-19 템플릿 포크(`dev`)에서 inject → verify_symptom 통과(로그인 500) → `kg check` S04 적발(critical)·경로 A → reset → H0 스모크 성공·verify_symptom 실패(로그인 401 = 정상 응답)까지 확인했다.
