# C4 — gateway PROXY_TIMEOUT 단축으로 채팅만 시간 초과

기획서 §8.3 확장 후보 C4(L3, 짧은 `PROXY_TIMEOUT` + 긴 응답). 분류 메모: 'K8s로 보임(Deployment env) × 일부 실패(긴 요청만)'.

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `C4_PROXY_TIMEOUT`(주입할 값 초, 기본 `1.2`), `TEST_ENV`(테스트 사용자 파일, 기본 `.env.test-user`).
전제: `make seed`. Chaos Mesh는 쓰지 않는다.

## 주입 내용

gateway Deployment에 `PROXY_TIMEOUT=1.2` env를 추가한다 (`kubectl set env`, 원래는 env가 없어 기본값 60초). gateway 코드(`core/config.py`의 `PROXY_TIMEOUT: float = 60.0`, `service/proxy.py`의 httpx `timeout`)가 이 값으로 업스트림 응답을 기다린다. 롤아웃이 끝나고 이전 Pod가 사라질 때까지 기다린다.

- 값은 실측으로 골랐다 (2026-11-18, 정상 상태, front-chat 경유): 로그인 0.87~1.04초(bcrypt), 채팅 1.33~1.86초(40회 이상), 짧은 API(`/admin/user/me`)는 0.04초. 로그인은 통과하고 채팅은 끊기도록 그 사이인 1.2초로 했다. 1.1초는 로그인(1.04초)에 여유가 없어 쓰지 않았다.
- 원래 값은 `.state/original_proxy_timeout`에 기록한다 (비어 있으면 env 없음). 원복은 값이 있었으면 복원하고 없으면 env를 제거한다.
- 이름에 시나리오 ID는 없지만 정답(`PROXY_TIMEOUT`)이 Deployment env에 그대로 보인다. 에이전트가 보는 `get_resource`는 env 값 없이 키만 보여 준다.

## 기대 증상

- gateway Pod는 Ready이고 재시작은 0이다 (새 Pod가 뜰 뿐이다). Service·ConfigMap 변경은 없다.
- 로그인(`/auth/login`)과 `/admin/user/me`는 성공한다. 채팅(`/agent/invoke`)은 gateway가 **504**로 끊는다. gateway는 시간 초과 시 한 번 더 시도하므로 채팅은 약 2.4초 뒤 504를 돌려준다.
- gateway 로그: `ServiceException: UPSTREAM_TIMEOUT | path=/api/v1/agent/invoke | detail=업스트림 서비스 응답 시간 초과: http://agent:8006/api/v1/invoke`와 `업스트림 요청 최종 실패: http://agent:8006/api/v1/invoke`.
- agent·llm-gateway 로그에는 오류가 남지 않는다.
- 규칙 점검: 장애 신호 없음 (규칙은 env 값을 판정하지 않는다). 경로 B는 기능 점검 `chat` 실패로 열린다. 사용자 증상 입력은 필요 없다.
- 가끔(40번 중 1번) 채팅이 시간 제한보다 빨리 끝나 200이 나온다. verify_symptom은 채팅을 3번 보내 2번 이상 504면 증상으로 본다.

## 실측 결과

2026-11-18 `make eval S=C4 ARGS="--in-cluster --condition ai"` 1회 통과 (실행 ID `20261118T134853Z-C4`, 4분 47초). 점검 32초 뒤 경로 B(적발 신호 없음), 원인 Deployment/gateway 일치, 근거 `gateway-upstream-timeout` 확보. 조사 결론은 설정값을 확인하지 못해 "추정"으로 적었지만 채점은 일치(근거 지지율 1.00). 원복 뒤 gateway env에 `PROXY_TIMEOUT`이 없고 H0 통과.

## 설계 메모

- 기획서 §8.3은 "긴 스트리밍 응답"이지만 비스트리밍 `/agent/invoke`를 쓴다. httpx 시간 제한은 청크 사이 읽기 기준이라 스트리밍이면 끊기지 않는다.

## 위험

- 로그인 쪽 여유: 로그인 응답 시간(최대 약 1.04초)과 값(1.2초)의 여유가 작다. 노트북이 느린 때는 로그인이 504로 끊겨 H0·verify가 실패할 수 있다 (그 경우 `C4_PROXY_TIMEOUT`을 올린다). 다만 gateway가 시간 초과를 재시도(`RETRY_MAX_ATTEMPTS=2`)하므로 로그인은 두 번 모두 넘어야 실패한다.
- 채팅 쪽 여유도 좁다: gateway가 재는 업스트림 시간은 클라이언트 측 1.33초보다 짧다. LLM이 빠를 때(40번 중 1번 200) 1회 기능 점검이 통과하면 경로 C로 흔들릴 수 있다.
- 토큰 검증(`auth/verify`)도 같은 시간 제한을 받는다.
