# A1 — Service selector 오타

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `A1_SERVICE`(기본 `agent`), `A1_KEY`(기본 `app`), `A1_POD_SELECTOR`(기본 `app=agent`). 템플릿 포크의 실제 Service 이름이 다르면 맞춰서 실행한다.

## 주입 내용

agent Service의 `spec.selector` 중 `app` 값 끝에 `x`를 붙인다 (`agent` → `agentx`). 원래 selector는 진단 대상 밖인 `scenarios/A1/.state/<네임스페이스>-<Service>.selector.json`(커밋하지 않음)에 저장하고 `reset.sh`가 되돌린다. 대상 객체에 흔적(annotation 등)을 남기면 조사 에이전트가 정답을 읽을 수 있어서다.

## 기대 증상

- agent Service의 EndpointSlice가 비고, gateway → agent 호출이 전부 실패한다 (agent 관련 기능 전부 실패).
- agent Pod는 Ready로 남는다.
- 규칙 점검: S01 즉시 적발(핵심 경로면 긴급), 게이트 경로 A.
- S02는 05-rules §5에서 "15분 뒤"로 적었지만, §4 S02 제외 조건("대상 Pod가 0개 (S01이 다룸)")으로 현재 구현에서는 통과(제외)로 나온다. 어느 쪽이 맞는지 확인이 필요하다 (#12 PR).

## 확인 상태

대상 앱(템플릿 포크, #7)이 아직 배포되지 않아 실제 agent Service에서는 돌려 보지 않았다. 임시 네임스페이스(`KG_NAMESPACE=kg-a1`, nginx로 만든 agent Deployment·Service)에서 inject → verify_symptom 통과 → `kg check` S01 적발·경로 A → reset → selector 원복·verify_symptom 실패(증상 없음)까지 확인했다. 템플릿 포크가 배포되면 실제 Service 이름·selector 키를 확인한다.
