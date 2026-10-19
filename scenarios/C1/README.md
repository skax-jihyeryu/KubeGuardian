# C1 — LLM 할당량 소진 (주입 보류)

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` · `verify_symptom.sh` · `reset.sh` | **아직 없다.** 아래 "막힌 이유" |

## 기획서의 의도 (§8.2)

- 증상: 채팅 실패. 원인: llm-gateway 할당량 소진 → 429. 주입: 할당량 하향. 기대 경로 B (API 시나리오).
- K8s 상태(Pod·Service·설정)는 모두 정상이고, llm-gateway의 할당량 API를 봐야 원인이 드러나는 L3 시나리오다.

## 막힌 이유

템플릿 llm-gateway는 할당량을 **저장만 하고 강제하지 않는다.** 할당량을 0이나 1로 내려도 채팅은 계속 성공한다. (2026-10-19, `~/Cursor/kg-target/backend-llm-gateway` 코드 확인)

| 근거 | 내용 |
|---|---|
| `app/service/chat_completion.py` 43~93행 `prepare_chat_completion` | 채팅 전 처리는 "LLM 라우터 인가 → 프롬프트 주입 → Provider 결정"뿐이다. 할당량을 읽지 않는다 |
| `app/service/llm_router.py` 23~36행 `check` | (조직, provider, model) 할당 여부만 보고 ALLOW/DENY(403)를 돌려준다 |
| `app/service/quota.py` 59~67행 `get_quota_usage` | `monthly_used`·`daily_used`·`*_remaining`을 항상 `None`으로 돌려준다. 사용량을 세지 않는다 |
| `app/api/routes/quota.py` 44행 | 주석 "used/remaining은 Redis 미연동으로 null" |
| `app/` 전체 `grep quota` | 할당량을 참조하는 곳은 `quota` 라우트·서비스·저장소뿐이다. 채팅 경로(`api/routes/chat_completions.py`)에는 없다 |

포크 코드는 이 작업에서 바꾸지 않기로 했으므로(진단 대상 수정은 F 목록으로 관리), 기획서 방식 그대로는 주입할 수 없다.

## 대안 (결정 필요, 명세 변경)

| 안 | 주입 | 바꿔야 할 것 | 원인의 의미 |
|---|---|---|---|
| (a) 포크에 할당량 강제 추가 | 할당량 API(`PATCH /api/v1/quotas/{org_id}`)로 `daily_limit` 하향, reset은 원래 값 복원 | llm-gateway 포크: 채팅 후 조직별 사용 토큰을 Redis에 누적(`api/routes/chat_completions.py`), 채팅 전 한도 초과면 429(`service/chat_completion.py`, `ErrorCode.RATE_LIMITED`), 사용량 API에 used/remaining 채우기(`service/quota.py`). 약 40~60줄 + 테스트. 실행 계획서 §1.1 F 목록에 추가 | 기획서 그대로 (할당량 소진). 할당량 API로 원인 확인 가능 |
| (b) HTTPChaos로 429 | llm-gateway Pod의 `/api/v1/chat/completions` 응답 코드를 429로 바꿈 (B1과 같은 방식) | 포크 변경 없음. 시나리오 스크립트만 (B1 수준 약 100줄). 기획서 §8.2 C1 행의 원인·주입 문구 수정 | "llm-gateway가 429를 돌려줌"까지만 맞힐 수 있다. 할당량 API 값은 정상이라 "할당량 소진"이라는 원인은 근거가 없다 |
| (c) 조직의 모델 할당 제거 | llm-gateway 할당 API로 `kg-test` 조직의 gpt-4.1 할당 제거 → 403, reset은 `make seed`와 같은 할당 복원 | 포크 변경 없음. 시나리오 스크립트 약 100줄. 기획서 §8.2 C1 행을 "모델 할당 누락 → 403"으로 수정 | 원인이 할당량이 아니라 설정(할당) 누락. 역시 K8s 밖 원인이라 L3·경로 B는 유지 |

## 함께 확인한 것

- 템플릿 agent는 LLM 호출이 실패해도 HTTP 200에 `오류가 발생했습니다: ...`를 answer로 돌려준다 (환경 설치 가이드 10단계). 기획서의 "gateway 502"는 실제로는 "200 + 오류 문구"로 보인다. 어느 안이든 verify_symptom은 이 문구로 판정해야 한다.
- 예시 설정 `kubeguardian.example.yaml`의 `app_apis.llm-gateway`는 `/api/v1/quota`인데 실제 라우트는 `/api/v1/quotas/{org_id}/usage`다 (`app/api/router.py` 22행).
