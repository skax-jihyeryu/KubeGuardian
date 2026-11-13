# E1 — 회복된 과거 재시작 (#30)

`entry → identity` 로그인 요청은 504로 끝납니다. 같은 요청 시각에 `identity`가
`sessionstore`에 세션을 쓰다가 연결 시간 초과를 기록합니다. 모든 Pod는 Ready입니다.
관찰 데이터, 환경 설명, 리소스 이름, 시각은 이 평가용으로 작성했습니다.

| 변형 | `thumbnailer-v1-pod` 관찰 |
|---|---|
| `clean` | Ready, restartCount 0 |
| `decoy` | Ready, restartCount 3 → 3, 약 5시간 전 OOMKilled, 그 직후 다시 실행 |

두 변형에서 현재와 직전 스냅샷으로 실제 P06 규칙을 평가하고 실제 게이트 판정과
InvestigatorV1 그래프와 운영 도구를 실행합니다. Kubernetes 읽기와 앱 HTTP 응답은
로컬 고정 데이터로 재생합니다. 두 변형에 같은 합성 환경 설명을 넣어 초기 구동 상태를
확인할 때 전체 Pod를 조회하도록 요청합니다. 이 조회가 수행되면 해당 Pod의 상태가
도구 응답에 포함됩니다. 합성 데이터에 없는 조회는 오류로 기록합니다. 클러스터 및 앱 API 호출이 없는 재생이므로 배포 환경의 장애 감지
시간과 경보율을 측정하지 않습니다.

## 재현

저장소 루트에서 실행합니다.

```sh
uv run pytest -q tests/eval/test_e1_replay.py
PYTHONPATH=app uv run python scenarios/E1/replay.py --variant clean --output /tmp/e1-clean.json
PYTHONPATH=app uv run python scenarios/E1/replay.py --variant decoy --output /tmp/e1-decoy.json
```

첫 명령은 각본형 모델을 사용합니다. 두 재생 명령은 설정된 Azure 모델을 호출하며,
보고서와 인용 근거, 게이트, P06 판정 및 채점 지표를 JSON에 저장합니다.

## 판정

- 두 변형 모두 P06 `passed`, 게이트 `B`가 기대값입니다.
- `decoy_observed`는 대상 Pod의 상태가 모델이 호출한 도구 응답에 포함됐는지 표시합니다. 두 변형에서 이 값이 참이어야 미끼 배제를 비교할 수 있습니다.
- `top1_expected`는 1순위 원인 리소스가 `identity` 또는 `sessionstore`에 해당하는지 확인합니다. `session_timeout_mentioned`는 결론이나 1순위 제목에 세션 저장과 시간 초과가 함께 언급됐는지 확인합니다. 최종 보고서와 인용 로그를 대조해 세션 저장 연결 시간 초과를 로그인 실패 원인으로 제시했는지 판정합니다.
- `decoy_rank1_resource`는 1순위가 미끼 리소스를 지목하는지 확인합니다. `decoy_claims_for_review`에는 결론과 1순위 제목 중 `thumbnailer`가 언급된 문장이 들어갑니다. 과거 재시작을 로그인 실패의 원인으로 지목한 문장만 오인으로 판정합니다. 회복 사실이나 원인 배제를 기록한 문장은 통과입니다.

## 실측 결과 (2026-11-13 KST)

Azure의 `gpt-6.1-sol`을 사용해 대조군과 미끼군을 각각 1회 실행했다.

| 변형 | P06 / 게이트 | 1순위 판단 | 미끼 배제 |
|---|---|---|---|
| 대조군 | passed / B | `Service/sessionstore` 연결 시간 초과 후보 | 해당 없음 |
| 미끼군 | passed / B | 세션 저장소 연결 시간 초과 후보, 원인 리소스 미확정 | 통과 |

미끼군의 Pod 조회 근거 `ev-0008`에는 `thumbnailer-v1-pod`의 재시작 횟수 3과
복구된 과거 종료 상태가 포함됐다. 1순위 리소스나 최종 원인 서술에 이 재시작을
지목하지 않았다. 구체 원인 리소스의 정답 일치는 미끼군에서 확인되지 않았다.
이 결과는 합성 관측값을 실제 모델에 재생한 1쌍의 결과다.
