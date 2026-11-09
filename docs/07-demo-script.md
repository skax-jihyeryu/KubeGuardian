# KubeGuardian AI — 시연 영상 데모 스크립트

> 2026-11-09 재배포 후 검증 기준이다. 이 문서는 A7 실행 절차를 다룬다. A7과 C8의 촬영 순서와 내레이션은 [시연 영상 스크립트](산출물/08-시연영상스크립트.md)를 따른다. 운영 로그 형식은 [README 운영 로그](../README.md#운영-로그), 화면은 [UI 정의서](04-ui-spec.md), 시나리오는 [`scenarios/A7/README.md`](../scenarios/A7/README.md)를 따른다.

## 1. 구성과 결정

| 항목 | 결정 |
|---|---|
| 중심 시나리오 | **A7 dot** (`http://agent:8006` → `http://agent.:8006`). 11/06 발표 피드백 "점·대시 오타로 인한 운영장애"에 직접 답한다 |
| A3 제외 | A3는 규칙(W02)이 주입 25~31분 뒤, P03은 약 1시간 뒤에야 적발해 영상에 담을 수 없다 ([A3 README](../scenarios/A3/README.md)). A7은 S04가 바로 적발한다 |
| AI 조사 장면 | **C8**. 기능 점검 실패 후 AI 조사와 보고서가 이어진다. [촬영 절차](산출물/08-시연영상스크립트.md) |
| 선택 촬영 | C1(Slack 흐름), B1(`kg investigate`). §6 참고 |
| 녹화 | 3개 창을 한 화면에 함께 녹화한다. 명령 입력용 작은 터미널(④)은 두지 않고, 시작 전에 ③ 창에서 주입·점검 명령을 쳐 두고 `-w`는 맨 아래 따로 띄운다 (아래 표) |
| 시작 방법 | 정시 점검을 기다리지 않고 `make kg-cron-run`으로 점검 Job을 바로 만든다 |

### 창 배치

| 창 | 내용 | 명령 |
|---|---|---|
| ① 화면 | 브라우저: 보고서 화면 `http://localhost:8080/llm-gateway/...` (필요하면 대상 서비스 front-chat 채팅 화면 탭 추가). Slack 테스트 채널은 별도 모니터나 탭 | `kubectl -n kubeguardian port-forward service/diagnostic-agent 8080:8080` (녹화 밖 터미널) |
| ② 작업자 로그 | 조사 흐름 한 줄씩 | `kubectl -n kubeguardian logs -f deploy/diagnostic-agent -c worker` |
| ③ Pod 감시 + 명령 | 위쪽 절반은 `kubectl get pods -n dev -w`, 아래쪽 절반(분할 터미널)은 `inject`·`make kg-cron-run`·`reset` 입력 | `kubectl get pods -n dev -w` |

3창으로 줄이는 이유: 4번째 창을 두면 글자가 작아져 영상에서 읽히지 않는다. ③의 아래쪽 분할에서만 명령을 친다.

## 2. 촬영 전 준비

| # | 확인 | 명령 / 기준 |
|---|---|---|
| 1 | 열린 이슈 없음 | 화면 이슈 목록에 gateway 열린 이슈가 없어야 한다. 같은 이슈가 열려 있으면 다음 점검이 `이슈 #N 진행 중 (같은 지문)`으로 끝나 조사를 다시 하지 않는다 |
| 2 | 정시 점검과 겹치지 않음 | CronJob은 `0 * * * *`. **정시 2분 뒤쯤 시작**해 다음 정시 전에 끝낸다 |
| 3 | 작업자 켜져 있음 | `make kg-cron-suspend`는 쓰지 않는다. 작업자도 멈춰 조사가 없다 |
| 4 | port-forward 살아 있음 | `make kg-up` 뒤에는 diagnostic-agent Pod가 바뀌어 연결이 끊긴다. 다시 연결한다 |
| 5 | 정상 상태 | `bash scenarios/H0/verify_symptom.sh`가 `OK` |
| 6 | Slack 테스트 채널 | 실제 채널로 알림이 간다. 채널을 화면에 띄워 둔다 |
| 7 | 원래 값 상태 파일 없음 | `scenarios/A7/.state/dev-gateway.env`가 없어야 inject가 돈다 |

## 3. 본 시나리오: A7

저장소 루트에서 ③ 아래쪽 터미널로 실행한다. 화면과 번호는 2026-11-09 재배포 후 검증 기록이며, 새로 실행하면 번호가 달라진다.

| 단계 | 창 | 명령 / 동작 | 확인할 결과 |
|---|---|---|---|
| 정상 확인 | ①, ③ | 이슈 목록 확인 후 `bash scenarios/H0/verify_symptom.sh` | 열린 이슈 없음, 정상 채팅 `OK` |
| 장애 주입 | ③ | `KG_TYPO=dot bash scenarios/A7/inject.sh` → `KG_TYPO=dot bash scenarios/A7/verify_symptom.sh` | gateway 롤아웃 완료, `AGENT_SERVICE_URL=http://agent.:8006`, 채팅 호출 500 |
| 점검 실행 | ③ | `make kg-cron-run` | 점검 #90, `게이트: 경로 A (critical) — 적발: S04`, `이슈 #15 새로 열림 (규칙 진단)` |
| 결과 확인 | ① | 이슈 상세 `/llm-gateway/issues/15` 및 점검 상세 `/llm-gateway/checks/90` | 규칙 기반 진단 결과에 `AGENT_SERVICE_URL=http://agent.:8006 → 'agent.:8006' 연결 대상 없음` 표시 |
| 근거 요약 | ② | 작업자 로그의 `근거 요약` 줄 | 경로 A에서도 근거 요약 LLM은 백그라운드에서 실행된다. 이번 점검의 근거 요약은 2건 완료됐고, 원인 조사 AI는 경로 B에서만 실행된다 |
| 원복·복구 | ③, ① | `bash scenarios/A7/reset.sh` → `bash scenarios/H0/verify_symptom.sh` → `make kg-cron-run` | 정상 채팅 `OK`, 점검 #91에서 경로 C와 이슈 #15 해결 |

## 4. 촬영 뒤 정리 (매 회)

1. `bash scenarios/A7/reset.sh`
2. `make kg-cron-run`으로 복구 점검을 실행하고 A7 이슈 해결과 복구 알림(M3)을 확인한다.
3. `bash scenarios/H0/verify_symptom.sh`가 `OK`인지 확인한다.

## 5. 실패 시 복구

| 증상 | 원인 | 복구 |
|---|---|---|
| inject가 거절됨 (상태 파일 있음) | 이전 주입이 원복되지 않음 | `bash scenarios/A7/reset.sh` 후 다시 inject |
| 점검이 `이슈 #N 진행 중 (같은 지문)`으로 끝나고 새 알림 없음 | 이전 촬영 이슈가 열려 있음 | reset → `make kg-cron-run`으로 해결시킨 뒤 처음부터 다시 |
| 이슈가 열리지 않음 | 주입이 반영되지 않았거나 H0 비정상 | `KG_TYPO=dot bash scenarios/A7/verify_symptom.sh`로 증상 확인. 열린 이슈 목록 확인 |
| 경로 B의 C8 조사가 시작되지 않음 (②에 `조사 대기 집음`이 없음) | 작업자가 멈춤 | `kubectl -n kubeguardian get deploy diagnostic-agent`로 Pod 확인. 작업자가 suspend로 멈췄다면 `make kg-cron-resume` |
| port-forward 8080이 이미 사용 중 | 이전 forward가 남음 | 이전 `kubectl port-forward` 프로세스를 종료하고 다시 연결 |
| 화면이 안 열림 (`make kg-up` 직후) | diagnostic-agent Pod가 바뀌어 연결이 끊김 | port-forward 재시작 |
| 정시 점검(`0 * * * *`)과 겹침 | 시작 시각이 정시 직전 | 중단하고 정시 2분 뒤에 처음부터 다시. 이슈 정리는 §4 |

## 6. 보조 시나리오 (선택 촬영)

| 시나리오 | 보여줄 것 | 순서 | 주의 |
|---|---|---|---|
| C1 (429 할당량 초과) | Slack 알림 흐름 | `bash scenarios/C1/inject.sh` → `make kg-cron-run` → Slack M1 → M1u → 화면 → `bash scenarios/C1/reset.sh` | Pod 변화가 없어 ③이 조용하다. 원복 때만 llm-gateway가 재시작된다. AI 원인은 3회 중 2회 맞았고 조사 약 165초 |
| B1 (agent Pod 하나 500) | `kg investigate "<증상>"` | `bash scenarios/B1/inject.sh` → CLI 실행 → `bash scenarios/B1/reset.sh` | 규칙 장애 신호가 없어 증상 입력이 경로 B를 연다. 증상 예: `채팅 요청이 간헐적으로 실패합니다.` (`scenarios/B1/expected.yaml`의 `user_symptom`) |

`kg investigate`는 `app/cli.py`의 `investigate` 하위 명령이다. Makefile 타깃은 없고 `check-run`처럼 직접 실행한다.

```bash
uv run python app/cli.py investigate "채팅 요청이 간헐적으로 실패합니다."   # 옵션: --config, --db, --verbose
```

CLI는 `--db` 기본값(`kubeguardian.db`)에 기록하므로 화면(diagnostic-agent의 `/data/kubeguardian.db`)에는 나타나지 않는다. B1 촬영은 터미널 출력(요약)을 보여 주고, 클러스터 안 runner Pod에서 실행하는 방법은 이번 리허설에서 확인하지 못했다.

## 7. 재배포 후 검증 결과 (2026-11-09)

| 시나리오 | 점검과 결과 | 복구 |
|---|---|---|
| A7 dot | 점검 #90, 경로 A, S04 적발. CLI 규칙 진단과 이슈·점검 상세의 문제 주소 문구 확인. 근거 요약 2건 완료, 원인 조사 0건 | H0 OK, 점검 #91에서 이슈 #15 해결 |
| C8 배포명 오류 | 점검 #92, 경로 B. 감사 로그의 `403 deployment_not_allowed` 확인. 조사 #16 보고서 화면 확인, 한자 검출 0건, 인용 근거 11개 | H0 OK, 점검 #93에서 이슈 #16 해결 |

| 발견한 문제 | 처리 |
|---|---|
| 열린 이슈가 남으면 같은 지문의 이슈가 계속됨 | 매 촬영 뒤 reset → 점검 1회로 이슈 해결 (§4) |
| 정시 CronJob과 겹칠 수 있음 | 정시 2분 뒤 시작. `make kg-cron-suspend`는 작업자도 멈추므로 쓰지 않음 |
| `make kg-up` 뒤 port-forward 끊김 | 재연결 (§2) |
| 작업자 로그에 `근거 요약` 줄, S04 줄에 긴 JSON | 규칙 진단 화면과 백그라운드 근거 요약을 보여 준다 (§3) |
| A3는 라이브 시연 불가 | A7로 중심 변경 (11/09 결정) |
| 이번 리허설에서 못 해 본 것 | C1·B1 촬영, B1 runner Pod 실행 |
