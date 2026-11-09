# C7 — 서비스 주소 호스트 끝의 점(.)·대시(-) 오타

발표 피드백(11/06) "점, 대시 추가로 인한 운영장애"를 설정값의 한 글자 오타로 해석한 시나리오다. 기존 18회 평가와 분리한 사전 시험이며 `EVAL_SCENARIOS`에는 넣지 않는다 (C6과 같다).

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |
| `lib.sh` | 공용 안전 검사·주입값 계산 |

환경 변수: `KG_NAMESPACE`(`dev`만 허용), `KG_TYPO`(`dot` 기본 / `dash`).

## 주입 내용

gateway Deployment의 `AGENT_SERVICE_URL` 호스트에만 한 글자를 더한다. 다른 env·리소스는 건드리지 않는다.

| `KG_TYPO` | 값 |
|---|---|
| (원래) | `http://agent:8006` |
| `dot` | `http://agent.:8006` |
| `dash` | `http://agent-:8006` |

원래 값은 `scenarios/C7/.state/dev-gateway.env`(커밋하지 않음)에 저장하고 `reset.sh`가 되돌린다.

## 안전

- 네임스페이스 `dev`, kube context `minikube`가 아니면 스크립트가 거절한다.
- 주입 중 재주입을 거절한다. 원래 값이 `http://agent:<포트>` 형식이 아니어도 거절한다.
- `reset.sh`는 현재 값이 저장된 원래 값이거나 주입값(dot·dash)일 때만 되돌린다. 그 밖의 값이면 덮어쓰지 않고 중단한다.
- 정기 점검 CronJob이 도는 시간대를 피한다. 실험 중에는 `make kg-cron-suspend`, 끝나면 반드시 `make kg-cron-resume`.

## 실행

저장소 루트에서 실행한다. 열린 이슈·조사 대기열이 없는지 먼저 본다.

```bash
bash scenarios/H0/verify_symptom.sh          # 정상이어야 한다 (OK)
KG_TYPO=dot bash scenarios/C7/inject.sh      # 또는 dash
KG_TYPO=dot bash scenarios/C7/verify_symptom.sh
# 규칙 점검만 (Slack으로 보내지 않고 별도 DB 사용)
SLACK_BOT_TOKEN= SLACK_APP_TOKEN= make check-run ARGS="--config kubeguardian.yaml --db /tmp/c7.db"
bash scenarios/C7/reset.sh
bash scenarios/H0/verify_symptom.sh
```

## 기대 증상

- Pod·Service는 정상이고 gateway는 Ready다. 로그인은 되고 채팅(`POST /agent/invoke`)만 500 `CONNECTION_ERROR`로 실패한다.
- gateway 로그: `http://agent.:8006/api/v1/invoke - [Errno -2] Name or service not known`.
- 규칙 점검: S04가 gateway를 적발(긴급, 채팅 핵심 경로), 게이트 경로 A. dot·dash 모두 같다.

## 확인 상태

2026-11-09 템플릿 포크(`dev`)에서 확인했다. 직전·직후 H0 스모크 OK.

| 변형 | verify_symptom | `kg check` (규칙만) | 게이트 | 비고 |
|---|---|---|---|---|
| dot (`agent.`) | 통과 (채팅 500) | S04 적발(critical), 토폴로지 엣지 `target=agent.` `unresolved` | A | 가설(S04가 끝 점을 놓침)은 틀렸다. 토폴로지가 호스트 전체(`agent.`)를 대상 이름으로 보므로 없는 Service로 판정한다 |
| dash (`agent-`) | 통과 (채팅 500) | S04 적발(critical), `target=agent-` `unresolved` | A | 예상대로 |

- 점검은 로컬 `kubeguardian.yaml`로 별도 DB에 실행했고 Slack은 dry-run이다. 로컬에는 테스트 계정 Secret이 없어 `증상 감지(chat)`는 확인하지 못함이었다. 채팅 실패는 verify_symptom(스모크)과 gateway 로그로 확인했다.
- AI 조건(dot 1회): `make eval S=C7 ARGS="--condition ai --in-cluster"` 통과. 게이트 경로 A·S04 적발 18초, 조사 결론이 gateway의 `agent.:8006` 호출 이름 해석 실패를 지목해 원인 일치·판정 일치, 근거 지지율 1.00. (로컬 실행은 테스트 계정 Secret이 없어 증상 감지가 확인하지 못함이 되어 무효였다. runner Pod에서 돌린다.)
- 두 변형 모두 reset 뒤 H0 스모크 OK, `AGENT_SERVICE_URL=http://agent:8006` 복원.

## eval 등록

층별 채점 대상(`EVAL_SCENARIOS`)에는 넣지 않는다. 기존 18회 성적과 분리하는 C6과 같은 원칙이고, 규칙(S04)이 바로 적발해 AI 가치를 가르는 시나리오가 아니다. 러너(`make eval S=C7`)로는 그대로 돌릴 수 있다 (기본은 dot).
