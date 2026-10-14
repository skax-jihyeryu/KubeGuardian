# XX — <시나리오 이름>

새 시나리오는 이 폴더를 `scenarios/<ID>/`로 복사해서 만든다. `expected.yaml`부터 쓴다.

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

## 주입 내용

## 기대 증상
