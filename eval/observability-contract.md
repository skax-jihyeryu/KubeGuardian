# 관측 가능성 계약 (#182)

에이전트가 도구로 볼 수 없는 주입 정보를 정답으로 요구하지 않으려고 둔다. 시나리오마다 관측할 수 있는 사실과 관측할 수 없는 사실, 인정하는 결론과 보류를 적는다.
채점 코드는 `eval/judge.py`의 `OBSERVABILITY_CONTRACTS`와 `eval/scoring.py`의 `assess_observability`다. 기존 엄격 채점(`expected.yaml`, pass·F 분류)은 그대로 두고 따로 기록한다.

## 계약 버전

| 버전 | 시점 | 내용 | 바꾼 이유 |
|---|---|---|---|
| v0 | ~2026-11-15 | `expected.yaml`의 cause·judge·핵심 근거만으로 채점 | — |
| v1 | 2026-11-16 (#271) | A1·C1·C2에 관측 판정 추가(`ai_runs[].observability`). 정확도·정확한 출처·미해결 충돌·과도한 확정·적절한/부적절한 보류를 따로 표시 | C1은 HTTPChaos가 앱 처리 뒤 응답을 바꿔 호출자 429와 서버 200이 함께 보인다. v0의 C1 1/3 통과를 원인 식별 성능으로 읽을 수 없었다 |
| v1 (보강) | 2026-11-18 | 관측 판정 1회당 시간(`seconds`) 기록, 사람 라벨과의 필드별 일치율 표시 | 추가 판정 비용이 조사 usage에 섞이지 않게 하고, judge의 한계를 표본으로 드러내려고 |

- `expected.yaml`과 정답 문구는 결과에 맞춰 바꾸지 않았다. v1은 판정을 **더하기만** 하고, 기존 F1·F3 분류와 과거 `eval/history/` 결과 파일은 덮어쓰지 않는다.
- 보고서는 기록의 `version`별로 행을 나눈다. v1 이전 묶음에는 관측 표가 나오지 않는다. 전후 비교는 같은 버전 행끼리만 한다.

## 시나리오별 계약

| 시나리오 | 성격 | 주입 위치 | 도구로 관측 가능 | 관측 불가 | 인정하는 결론 | 인정하는 보류 |
|---|---|---|---|---|---|---|
| A1 | 관측 가능한 설정 오류 | agent Service `spec.selector` | Service selector와 Pod 라벨(`get_resource`), 빈 엔드포인트 | — | selector 불일치를 근거로 확정 | 없음. 근거가 있는데 보류하면 **부적절한 보류** |
| C1 | 관측 충돌 | Chaos Mesh HTTPChaos가 llm-gateway 응답을 429로 교체 (진단 대상 밖) | agent 로그의 llm-gateway 대상 429·`insufficient_quota` 본문, llm-gateway 직접 호출 429 | llm-gateway 자기 로그의 429 (교체가 앱 처리 뒤라 남지 않음), 같은 요청의 서버 상태(요청 ID 없음), 실제 할당량 소진 | llm-gateway 쪽에서 429가 온다는 수신 경로. 생성 지점은 가능성으로 남김 | 최초 생성 지점·할당량 소진을 확정하지 않고 충돌(호출자 429 / 서버 200)을 드러낸 보류 = **적절한 보류** |
| C2 | 관측 제한 | admin → redis NetworkChaos | 로그인 시간 초과(504), admin 응답 정체(앱 API·연결 확인) | admin 출발 redis 연결 실패 로그 (연결이 걸려 오류 줄이 남지 않음, `key_evidence.obtainable: false`) | admin 정체, 근거에 맞는 하위 의존성 대기 | Redis 차단을 확정하지 않은 보류 = **적절한 보류**. 추정만으로 Redis 차단을 확정하면 **과도한 확정** |

나머지 시나리오(A3·A5·A6·A7·B1·C8·E1·H0·H1)는 아직 v1 계약이 없다. 기존 엄격 채점만 적용한다.

### 판정 규칙 요약

- **정확도(accuracy)**: 기존 근거 채점이 결론을 supported·partial로 보고, 관측 판정이 supported·compatible일 때.
- **정확한 출처(exact_origin_supported)**: 위에 더해 결론이 supported이고 실제 원인 위치·동작을 직접 근거로 댄 경우. 의미 judge가 일치했다고 cause 필드 위치까지 일치했다고 보지 않는다 (cause 필드 일치는 기존 `cause_match`).
- **과도한 확정**: `judgement=confirmed`인데 정확한 출처가 아니거나 미해결 충돌이 있음.
- **부적절한 보류**: A1처럼 정답을 알 수 있는 사례의 보류, 또는 근거 없는 빈 보류. 모든 candidate·null을 성공으로 치지 않는다.
- 회귀 테스트: `tests/eval/test_observability.py::test_contract_distinguishes_visible_limited_and_conflict_cases`.

## C1의 두 목적 분리

C1은 두 가지를 동시에 재고 있었다. 이 둘을 나눈다.

1. **충돌 관측 진단 (현재 C1 그대로)**: HTTPChaos 응답 교체. 모순 인식과 합리적 보류를 평가한다. 정답 파일·주입은 바꾸지 않는다.
2. **오류 출처 진단 (새 변형, 미구현)**: 상위 모의 서버가 실제로 429를 만들고 요청 ID와 함께 로그를 남긴다. 같은 요청의 생성 근거를 찾아 출처를 확정하는지 평가한다.

새 변형의 조건:

- 실제 할당량을 쓰지 않는 모의 응답(`insufficient_quota` 본문과 같은 모양)이며, README에 모의 장애라고 적는다.
- 모의 서버는 요청마다 요청 ID·경로·응답 코드를 로그에 남긴다. llm-gateway가 그 응답을 받아 전달하므로 llm-gateway 로그에도 429가 남아야 한다.
- 주입 이름·상태 파일·Chaos 리소스처럼 정답이 드러나는 것은 도구 입력에 넣지 않는다 (C1·B1과 같은 원칙).
- 템플릿 llm-gateway는 LLM 배포(엔드포인트·키·모델)를 DB에 두고 조직별로 할당한다. 모의 서버를 상위로 쓰려면 배포 등록·원복 절차를 진단 대상 환경에서 확인해야 해서 이 컨테이너에서는 만들 수 없다. 시나리오 ID는 fixture를 구현할 때 #81 형식(단계 필드)에 맞춰 정한다. 전체 ID 개편은 하지 않는다.

## 사람 검토 라벨

`eval/labels/<묶음 디렉터리>.yaml`의 회차 항목에 `observability`를 선택으로 붙인다.

```yaml
runs:
  - index: 0
    top1_match: false
    claims: []
    observability:          # 사람이 본 관측 판정. 적은 필드만 비교한다
      finding: compatible   # supported | compatible | contradicted | unsupported
      unresolved_conflict: recognized   # none | recognized | missed
      abstained: true
      exact_origin_supported: false
```

`eval/report.py`는 관측 기록이 있는(채점 오류가 아닌) 회차만 필드별 `일치/라벨 수`로 보여 준다. 관측 판정 값은 라벨로 덮어쓰지 않는다. 표본이 작으므로 일치율은 judge가 어디서 틀리는지 보는 용도이고, 관측 표 수치를 보정하지 않는다.

### judge의 한계

- 결론 문장과 기존 주장별 근거 판정 요약만 보고, 근거 원문은 다시 보내지 않는다. 요약이 빠뜨린 관측은 판정에도 빠진다.
- 계약이 설명하는 관측 한계와 그 회차가 실제로 관측한 사실은 다르다. 같은 C1이라도 회차마다 본 로그가 다르다.
- 대상 회차마다 모델을 한 번 더 부른다. 그 시간은 `observability.seconds`에 남고 조사 `usage`(시간·토큰)에는 들어가지 않는다. 토큰은 아직 기록하지 않는다.

## 실행 기록에서 함께 보는 것

관측 표의 수치는 회차 유효성(주입·증상 확인·원복 성공), 환경 드리프트 검사(`batch_drift`), 조사 시간·토큰과 함께 읽는다. 무효 회차(F0·주입 실패)는 기존 규칙대로 분모에서 빼고, 관측 기록이 없는 조사 회차는 실패로 센다.
