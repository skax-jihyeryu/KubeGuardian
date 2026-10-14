# eval

평가 러너, 채점, 리포트. `kg` CLI와 SQLite 결과만 사용한다.

- `make eval S=<ID>`: 시나리오 하나 (주입 → 증상 확인 → 점검·게이트·AI → 채점 → 원복)
- `make eval`: 전체 시나리오 × 2조건 × 3회
- 결과는 `eval/history/`에 쌓는다 (커밋하지 않음)
