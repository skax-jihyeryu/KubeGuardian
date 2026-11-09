# C8 — 잘못된 LLM 호출 배포명

발표용 추가 장애 시나리오 7번. 기존 11/3 장애 6종·18회 평가와 분리한 사전 시연 시험이다.

## 장애와 정답

테스트 서비스 `dev`의 PostgreSQL `llm_deployment`에서 활성 Azure `gpt-4.1` 행의 `deployment_name`만 `gpt-4.1-next`로 변경한다. 사용자 요청의 모델명, 키, endpoint, 조직 할당과 Kubernetes 리소스는 변경하지 않는다.

현재 환경은 LLM 게이트웨이가 허용되지 않은 배포명을 **403 `deployment_not_allowed`**로 거절한다. 잘못된 모델/배포명을 사용한 실제 상류 호출 실패이며 HTTPChaos 응답 치환이 아니다. Azure의 404 `DeploymentNotFound`를 재현했다고 설명하지 않는다. 다른 상류 endpoint에서는 결과가 달라질 수 있어 이 검증기는 현재 dev 환경 전용이다.

Pod·Service는 정상인 채 채팅만 실패한다. agent는 사용자 응답에 일반 오류를 표시할 수 있으므로 상세 코드/사유는 llm-gateway 서버 로그로 확인한다. 규칙 장애 신호 없이 핵심 기능 점검 실패로 AI 조사 분기에 진입해야 한다. DB 변경 이력은 평가 정답 보관용이며 에이전트에 제공하지 않는다. 에이전트가 직접 SQL로 DB를 조회하는 기능은 없다.

## 실행

저장소 루트에서 실행한다. 현재 클러스터가 테스트 `dev`인지, 정상 로그인·채팅, 열린 이슈·조사 대기열이 없는지 먼저 확인한다. 키·비밀번호는 기존 `.env.test-user`와 클러스터 설정을 사용하며 출력하지 않는다.

```bash
bash scenarios/H0/verify_symptom.sh
bash scenarios/C8/inject.sh
bash scenarios/C8/verify_symptom.sh
```

이후 서비스의 **직접 점검**을 누른다. 기능 실패 → 새 이슈/최초 알림 → AI 조사 단계/원인 보고서 → Phoenix trace를 확인한다. 실제 테스트 Slack 채널에 알림이 전송될 수 있다. 정기 점검과 겹치지 않도록 매시 정각을 피해 짧게 진행한다.

성공 여부와 관계없이 반드시 원복하고 직접 점검을 다시 실행한다.

```bash
bash scenarios/C8/reset.sh
bash scenarios/H0/verify_symptom.sh
```

직접 점검에서 기능 정상·이슈 해결·복구 알림을 확인한다. 정상화는 DB 원복 즉시 실제 호출로 확인하고, 이슈 해결은 후속 점검으로 확인한다. 원복은 Pod 재시작이 필요 없다.

## 저장·검증

- 원래 배포명·행 ID는 `.state/dev-deployment.json`에 보관하고 원복 후 제거한다. 비밀 키·endpoint를 저장하지 않는다.
- 주입 중 재실행을 거절한다. 복구 시 해당 행이 예상 주입값과 다르면 덮어쓰지 않고 중단한다.
- 검증기는 DB 주입 상태, 대상 Pod Ready, 실제 채팅 실패, 주입 이후 llm-gateway 서버 측 감사 로그(`llm_audit_log`)의 403·거절 코드·배포명을 모두 확인한다. 정상 환경에서는 실패해야 한다.
- 1회 사전 시험 성공은 3회 반복 정확도나 기존 KPI 달성을 뜻하지 않는다.

## 사전 시험 결과

이슈 #188에 기록했다.

## 확인 상태

2026-11-09 #232: 검증기가 파드 로그(`kubectl logs`)에서 `status=403`·`deployment_not_allowed`를 찾았으나, 실제 파드 로그에는 둘 다 없다. 파드 로그에는 SQLAlchemy 감사 로그 INSERT 줄(`'deployment_name': 'gpt-4.1-next'`, `'response_status': 'error'`)과 `POST /api/v1/chat/completions ... 500`만 있고 `log_data`는 `... (N characters truncated) ...`로 잘린다. 403 근거(`403: Azure OpenAI API 호출 실패 (403): {"error": {"code": "deployment_not_allowed", ...}}`)는 `llm_audit_log.log_data`에만 온전히 남는다. 검증기를 이 테이블 조회로 바꿨다. 에이전트에는 여전히 DB 조회 수단이 없다.
