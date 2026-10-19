# H0 — 정상 기준선

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 기대 결과: 장애 신호 없음, 게이트 경로 C |
| `inject.sh` · `reset.sh` | 하지 않음 |
| `verify_symptom.sh` | 정상 동작 확인 (`scripts/target/smoke.sh`: front-chat 경유 로그인 → 채팅) |

전제: `make up` → LLM 배포 등록 → `make seed` (환경 설치 가이드 10단계).

## 기대 결과

- 장애 신호(N·P·W·S·C 규칙)는 적발 0건이고 확인하지 못함도 없다.
- 개선 권고 H01이 6건 나온다. 템플릿의 readinessProbe가 모두 tcpSocket이기 때문이다 (postgres·redis 제외).
- 게이트 경로 C.

## 확인 상태

2026-10-19 `kg check`(kubeguardian.example.yaml)로 위 결과를 확인했다.
