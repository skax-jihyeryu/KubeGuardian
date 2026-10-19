# C2 — admin → Redis 연결 차단

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `KG_CHAOS_NS`(Chaos 객체를 만들 네임스페이스, 기본 `chaos-mesh`), `TEST_ENV`(테스트 사용자 파일, 기본 `.env.test-user`).
전제: `make seed`로 테스트 사용자가 있어야 한다 (verify_symptom이 실제 계정으로 로그인한다. 틀린 비밀번호는 Redis에 닿기 전에 401로 끝나기 때문이다).

## 주입 내용

Chaos Mesh NetworkChaos(`action: partition`, `direction: both`)로 admin Pod와 redis Pod 사이 패킷을 버린다. 객체는 `chaos-mesh/NetworkChaos/kg-c2-<네임스페이스>`로, 진단 대상 네임스페이스 밖에 만든다. 대상 객체에 정답이 보이지 않게 하기 위해서다.

- `direction: both`인 이유: admin은 Redis를 Service ClusterIP로 부른다. admin Pod 안에서는 목적지가 Pod IP가 아니라 ClusterIP라, admin 쪽 규칙만으로는 맞지 않을 수 있다. redis Pod 쪽에서도 admin Pod IP를 막아 확실히 끊는다.
- Chaos Mesh는 대상 Pod 네임스페이스(`dev`)에 PodNetworkChaos 객체를 만든다. 원복 뒤에도 spec이 빈 채로 남는다 (Chaos Mesh 동작). KubeGuardian은 이 kind를 수집하지 않는다.

## 기대 증상

- 테스트 사용자의 front-chat 경유 로그인이 약 120초 뒤 504로 실패한다. admin이 로그인 세션을 Redis에 저장(`auth/service.py`의 `redis.hget`·`hset`)하다 응답 없이 멈추고, gateway가 `PROXY_TIMEOUT` 60초 × 2회 시도 뒤 504를 돌려준다. 이미 받은 토큰의 검증도 Redis를 보므로 채팅 등 인증이 필요한 API도 실패한다 (admin `core/security.py` 코드 기준, 실행으로는 로그인만 확인).
- admin·redis·gateway·front-chat Pod는 모두 Ready다 (readinessProbe가 tcpSocket·`redis-cli ping`). redis 자체는 `PONG`으로 응답한다.
- admin 로그에는 오류가 남지 않는다 (예외가 아니라 응답 대기에서 멈춤).
- 규칙 점검: 장애 신호 없음, 경로 C. 기대 경로 B는 증상 감지(API 시나리오 로그인 실패)가 연결된 뒤에 나온다.

## 확인 상태

2026-10-19 템플릿 포크(`dev`)에서 확인했다.

- inject → AllInjected → verify_symptom 통과 (로그인 504, 약 121초). 같은 상태에서 `kg check`는 장애 신호 없음, 경로 C.
- reset → NetworkChaos 삭제(1초 안), verify_symptom 실패(로그인 200 = 증상 없음), H0 스모크 성공.
- `make eval S=C2`: 실패(경로 C, 기대 B). 증상 감지가 아직 없어 예상된 결과다. 원복·H0 복구는 성공, 전체 238초 (증상 확인 1회에 약 120초).
