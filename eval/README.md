# eval

평가 러너, 채점, 리포트. `kg` 점검 흐름과 SQLite 결과만 사용한다.

- `make eval S=<ID>`: 시나리오 하나 (주입 → 증상 확인 → 점검·게이트 → 채점 → 원복). 지금은 기준 조건(규칙만)이고, AI 조사 단계는 W2에 붙인다
- `make eval`: 전체 시나리오 × 2조건 × 3회 (W2 이후)
- 결과는 `eval/history/<시각>-<ID>/`에 `result.json`과 점검 DB로 쌓는다 (커밋하지 않음)

## 러너 (`run.py`)

| 단계 | 하는 일 | 실패하면 |
|---|---|---|
| 주입 | `scenarios/<ID>/inject.sh` | 무효 |
| 증상 확인 | `verify_symptom.sh`가 성공할 때까지 반복 (기본 180초) | 무효 (증상이 안 나면 그 실행은 믿을 수 없다) |
| 점검·게이트 | `run_check` 한 번 | — |
| 채점 | `scoring.py` | — |
| 원복 | `reset.sh` 후 H0 `verify_symptom.sh`(로그인 → 채팅)로 복구 확인 | 무효 |

종료 코드: 0 통과, 1 실패, 2 무효. 전제는 진단 대상이 정상 기준선(H0)인 상태다 (환경 설치 가이드 10단계).

## 채점 (`scoring.py`, 기준 조건)

- 통과 = `expected.yaml`의 `expected_signals`를 모두 적발 + 게이트 경로가 `gate_path`와 같음
- 개선 권고(H 규칙)는 장애 신호로 세지 않고 `advice`로 따로 남긴다
- 기대 밖 적발(`unexpected`, 오탐 후보), 관찰 중(`observing`, 지속 시간 전), 확인하지 못함(`unknown`)은 기록한다
- 점검은 주입 직후 한 번이다. 지속 시간이 있는 규칙(S02 등 15분)은 "관찰 중"으로 나온다
