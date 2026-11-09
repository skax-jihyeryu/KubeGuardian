# KubeGuardian AI — 시연 영상 데모 스크립트

> 2026-11-09 클러스터 리허설 기준이다. 영상은 **C7(서비스 주소 점·대시 오타)** 중심으로 찍는다. 운영 로그 형식은 [README 운영 로그](../README.md#운영-로그), 화면은 [UI 정의서](04-ui-spec.md), 시나리오는 [`scenarios/C7/README.md`](../scenarios/C7/README.md)를 따른다.

## 1. 구성과 결정

| 항목 | 결정 |
|---|---|
| 중심 시나리오 | **C7 dot** (`http://agent:8006` → `http://agent.:8006`). 11/06 발표 피드백 "점·대시 오타로 인한 운영장애"에 직접 답한다 |
| A3 제외 | A3는 규칙(W02)이 주입 25~31분 뒤, P03은 약 1시간 뒤에야 적발해 영상에 담을 수 없다 ([A3 README](../scenarios/A3/README.md)). C7은 S04가 바로 적발한다 |
| 보조 시나리오 | C1(Slack 흐름), B1(`kg investigate`). 선택 촬영 (§6) |
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
| 2 | 정시 점검과 겹치지 않음 | CronJob은 `0 * * * *`. **정시 2분 뒤쯤 시작**해 다음 정시 전에 끝낸다 (전체 약 2분 20초) |
| 3 | 작업자 켜져 있음 | `make kg-cron-suspend`는 쓰지 않는다. 작업자도 멈춰 조사가 없다 |
| 4 | port-forward 살아 있음 | `make kg-up` 뒤에는 diagnostic-agent Pod가 바뀌어 연결이 끊긴다. 다시 연결한다 |
| 5 | 정상 상태 | `bash scenarios/H0/verify_symptom.sh`가 `OK` |
| 6 | Slack 테스트 채널 | 실제 채널로 알림이 간다. 채널을 화면에 띄워 둔다 |
| 7 | 원래 값 상태 파일 없음 | `scenarios/C7/.state/dev-gateway.env`가 없어야 inject가 돈다 |

## 3. 본 시나리오: C7 (약 2분 20초)

저장소 루트에서 ③ 아래쪽 터미널로 실행한다. 시각은 리허설(16:18) 실측이다.

| 시각 | 단계 | 창 | 명령 / 동작 | 예상 화면 | 내레이션 |
|---|---|---|---|---|---|
| 0s | 정상 확인 | ① | 이슈 목록·점검 이력 | 열린 이슈 없음 | "지금은 모든 서비스가 정상입니다." |
| 0s | 장애 주입 | ③ | `KG_TYPO=dot bash scenarios/C7/inject.sh` | 출력에 주입 값 | "gateway가 agent를 부르는 주소에 점 하나만 더했습니다." |
| +13s | 배포 반영 | ③ | (자동) | gateway 새 Pod `Pending → ContainerCreating → Running`, 이전 Pod `Terminating` | "Pod는 정상 Ready라 Kubernetes는 아무 이상도 알리지 않습니다." |
| +13s | 점검 실행 | ③ | `make kg-cron-run` | Job 생성 | "정시를 기다리지 않고 점검을 지금 실행합니다." |
| +22s | 규칙 점검 완료 | ① 또는 Job 로그 | 점검 상세 `/llm-gateway/checks/<run>` | `S04 서비스 주소 설정 오류 적발(critical) dev/Service/gateway`, `통과 99건 · 규칙 14개`, 증상 `chat 2단계 중 2단계 실패 (call /api/v1/agent/invoke → 500)`, `게이트: 경로 A (critical) — 적발: S04`, `이슈 #11 새로 열림 (조사 대기)` | "규칙이 서비스 주소 오류를 바로 잡고, 실제 채팅 호출도 500으로 실패했습니다." |
| +22s | 1차 알림 | Slack | | `[알림] M1 발송` 이슈 알림 | "Slack에는 먼저 짧은 알림이 갑니다." |
| +25s | 조사 시작 | ② | (자동) | `[작업] 조사 대기 집음` → `[조사 #13] 시작: 이슈 #11 경로 A` → `사전 확인: 근거 2건` → `선별: 문제 1개 — 채팅 호출 실패 및 gateway→agent 서비스 주소 미해결` → `계획: 가설 3개` | "작업자가 조사를 집어 사실을 모으고 가설 3개를 세웁니다." |
| +30~100s | 도구 검증 | ②, ① | | `도구 …` 8줄 안팎: run_api_scenario chat, get_resource Deployment/gateway·Service/agent, Pod app=agent·app=gateway, EndpointSlice, probe agent, get_pod_logs gateway. 점검 상세의 단계가 실시간 갱신 | "가설마다 도구로 직접 확인합니다. 화면에도 단계가 올라옵니다." |
| +109s | 결론 | ② | | `가설 판정: … 지지 / …` → `결론: Deployment/gateway가 agent 호출 대상을 http://agent.:8006으로 사용하여 이름 해석에 실패…` → `[알림] M1u 발송`, `[알림] M9 발송` → `조사 마침: done` | "원인은 호스트 끝의 점 하나, gateway가 `agent.`를 해석하지 못한다는 결론입니다." |
| +109s | 보고서 | ① | 이슈 상세 `/llm-gateway/issues/<n>` | 결론과 근거 링크, 판단 확정, 문제 표, 사전 확인(get_topology: unresolved 1개 gateway→agent.), 가설 판정 표(지지/미결/배제), 확인한 범위 | "근거 번호를 눌러 원문을 확인할 수 있고, 배제한 가설과 확인한 범위도 남습니다." |
| +109s | Slack | Slack | | M1 → M1u(원인 갱신) → M9 스레드 답글 | "같은 메시지가 원인 알림으로 갱신되고 스레드에 보고서 링크가 붙습니다." |
| +115s | 원복 | ③ | `bash scenarios/C7/reset.sh` | gateway 재배포 | "주소를 원래대로 되돌립니다." |
| +125s | 복구 확인 | ③ | `make kg-cron-run` | `게이트: 경로 C — 장애 신호 없음`, `이슈 #11 해결`, `[알림] M3 발송` | "다음 점검에서 정상으로 판정하고 복구 알림이 갑니다." |
| +138s | 마무리 | ①, Slack | | 이슈 해결됨, 복구 알림 | |

### 영상에서 설명할 점

| 보이는 것 | 설명 |
|---|---|
| ②에 `근거 요약: ev-… done 2.1초 …` 줄이 섞임 | 근거마다 AI 요약을 백그라운드로 만드는 줄이다. 조사와 별개라고 말하고 넘어간다 |
| S04 요약 줄에 주소가 담긴 긴 JSON | 점검이 본 서비스 주소 전체다. 필요하면 확대해 `agent.` 부분을 가리킨다 |
| 조사는 약 90초 | LLM과 도구 호출 시간이다. 편집으로 줄이지 않거나, 줄였다면 자막으로 알린다 |

## 4. 촬영 뒤 정리 (매 회)

1. `bash scenarios/C7/reset.sh`
2. `make kg-cron-run` 한 번으로 이슈를 해결(M3)시킨다. 열린 채 두면 다음 촬영에서 조사가 다시 돌지 않는다.
3. `bash scenarios/H0/verify_symptom.sh`가 `OK`인지 확인한다.

## 5. 실패 시 복구

| 증상 | 원인 | 복구 |
|---|---|---|
| inject가 거절됨 (상태 파일 있음) | 이전 주입이 원복되지 않음 | `bash scenarios/C7/reset.sh` 후 다시 inject |
| 점검이 `이슈 #N 진행 중 (같은 지문)`으로 끝나고 조사 없음 | 이전 촬영 이슈가 열려 있음 | reset → `make kg-cron-run`으로 해결시킨 뒤 처음부터 다시 |
| 이슈가 열리지 않음 | 주입이 반영되지 않았거나 H0 비정상 | `KG_TYPO=dot bash scenarios/C7/verify_symptom.sh`로 증상 확인. 열린 이슈 목록 확인 |
| 조사가 시작되지 않음 (②에 `조사 대기 집음`이 없음) | 작업자가 멈춤 | `kubectl -n kubeguardian get deploy diagnostic-agent`로 Pod 확인. 작업자가 suspend로 멈췄다면 `make kg-cron-resume` |
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

## 7. 리허설 결과 (2026-11-09, 재배포 후)

| 항목 | 결과 |
|---|---|
| 시나리오 | C7 dot, 깨끗한 상태에서 1회 끝까지 (16:18 실행) |
| 소요 시간 | 주입 0s → gateway 롤아웃 +13s → 점검 Job 완료 +22s → 조사 완료 +109s → reset·복구 점검 포함 전체 +138s |
| 규칙 점검 | S04 적발(critical), 통과 99건·규칙 14개, 게이트 경로 A, 이슈 #11 열림 |
| 조사 | 조사 #13: 근거 2건, 문제 1개, 가설 3개, 도구 약 8회, 결론이 `agent.:8006` 이름 해석 실패를 지목 |
| 알림 | M1 → M1u → M9 → (복구) M3 모두 발송 |

| 발견한 문제 | 처리 |
|---|---|
| 열린 이슈가 남으면 다음 점검이 조사를 다시 하지 않음 | 매 촬영 뒤 reset → 점검 1회로 이슈 해결 (§4) |
| 정시 CronJob과 겹칠 수 있음 | 정시 2분 뒤 시작. `make kg-cron-suspend`는 작업자도 멈추므로 쓰지 않음 |
| `make kg-up` 뒤 port-forward 끊김 | 재연결 (§2) |
| 작업자 로그에 `근거 요약` 줄, S04 줄에 긴 JSON | 문제 아님. 설명 포인트로 정리 (§3) |
| A3는 라이브 시연 불가 | C7로 중심 변경 (11/09 결정) |
| 이번 리허설에서 못 해 본 것 | C1·B1 촬영, B1 runner Pod 실행 |
