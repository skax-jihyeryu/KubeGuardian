# 원인 특정 실패 정리 (eval 이력, 10/19~11/10)

> 근거 데이터: AI 조사 완료 회차 115회(`scratchpad/kpi/runs.jsonl`)를 원인 지점 기준으로 다시 채점한 결과(`scratchpad/kpi/rescore.csv`). 시나리오 정답은 `scenarios/<ID>/expected.yaml`이다.

## 1. 요약

- 엄격 기준 실패는 115회 중 62회다(엄격 통과 53회).
- 엄격 실패 62회 중 원인 지점 기준으로는 정답인 회차가 57회, 오답이 5회다. 전체로는 엄격 53/115, 원인 지점 110/115다.
- 실패 유형별 회차 수는 다음과 같다.

| 유형 | 회차 | 원인 지점 기준 |
|---|---|---|
| 호출자·전달자 지목(C1 agent) | 22 | 정답 22 / 오답 0 |
| 원인 리소스 필드 비어 있음(C1) | 8 | 정답 8 / 오답 0 |
| 경로 Service 지목(C1 llm-gateway-svc) | 3 | 정답 3 / 오답 0 |
| 하위 의존성 미규명(C2 admin까지만) | 24 | 정답 24 / 오답 0 |
| 원인 미발견(B1) | 2 | 정답 0 / 오답 2 |
| 잘못된 원인 | 3 | 정답 0 / 오답 3 |

- 실패의 대부분(C1·C2)은 원인 지점까지는 찾았고, 그 안쪽(429를 만든 리소스, admin이 멈춘 이유)을 특정하지 못한 경우다. 원인 지점 자체를 놓친 오답은 B1 4회(w4-final 3회, cb91f5d 1회)와 C2 1회(readiness 설정을 원인으로 지목)다.

## 2. 채점 기준 두 가지

- **엄격 기준**: 보고서의 1순위 원인 리소스와 결론이 `expected.yaml`의 `expected_cause`·`root_cause`와 일치해야 한다. 원인 Pod·리소스까지 정확히 맞아야 통과한다.
- **원인 지점 기준**: 보고서가 (a) 시나리오의 원인 신호를 근거로 찾았고, (b) 1순위로 원인 서비스 또는 그와 호출 관계로 직접 연결된 서비스를 지목했으며, (c) 원인을 평가용 설정 변경이나 무관한 서비스·설정으로 돌리지 않았으면 정답이다. 원인 Pod·리소스를 정확히 특정하지 않아도 되고, 확정/후보 여부는 채점에 쓰지 않는다.
- 시나리오별 허용 지목: A1 agent(Service/Deployment)·gateway, A3 agent, A5 gateway 또는 하위 서비스, B1 agent Pod·agent, C1 llm-gateway·agent, C2 admin·redis·gateway(readiness 설정 변경을 원인으로 돌리면 오답).
- 판단이 갈린 회차는 `rescore.csv`의 `ambiguous` 열에 표시했고(9회), 모두 정답/오답 중 하나로 결정했다.

## 3. 배치별 표

| batch | 날짜 | 엄격 | 원인 지점 |
|---|---|---|---|
| w4-final | 20261102 | 11/18 | 15/18 |
| w4-hardening-m3 | 20261102 | 3/8 | 8/8 |
| w4-hardening-m4 | 20261102 | 9/15 | 15/15 |
| w4-c1-tools-vs-recheck | 20261102 | 1/6 | 6/6 |
| w4-cause-chain-146 | 20261102 | 6/8 | 8/8 |
| w4-retest-20261103-cb91f5d | 20261102 | 11/18 | 17/18 |
| (미지정) | 20261109 | 1/1 | 1/1 |
| w5-c1-recheck | 20261109 | 2/3 | 3/3 |
| w5-75-base-c458dd6-r3 | 20261109 | 1/3 | 3/3 |
| w5-75-tuned-c458dd6-r3 | 20261109 | 0/3 | 3/3 |
| w5-75-base-c458dd6-c2-tcp | 20261109 | 0/2 | 2/2 |
| w5-75-base-c458dd6-c2-tcp-isolated | 20261109 | 0/3 | 3/3 |
| w5-75-tuned-c458dd6-c2-tcp-isolated | 20261109 | 2/3 | 3/3 |
| w5-final-kpi-f97c068 | 20261109 | 4/6 | 6/6 |
| w5-final-kpi-f97c068-c2-tcp | 20261109 | 0/3 | 2/3 |
| w5-246-base | 20261109 | 1/6 | 6/6 |
| w5-246-edge | 20261109 | 0/3 | 3/3 |
| w5-246-edge2 | 20261109 | 1/6 | 6/6 |
| 합계 | | 53/115 | 110/115 |

`(미지정)`은 batch 정보가 없는 C7 단독 회차다(현재 이름 A7에 해당, gateway의 agent 주소 오류).

## 4. 실패 유형 분류와 시나리오별 정리

### 4.1 호출자·전달자 지목 (C1 agent)

22회. llm-gateway가 돌려준 429를 agent가 오류 답변으로 전달하는 지점을 1순위로 지목했다. 429 신호는 찾았으므로 원인 지점 기준으로는 정답이다. agent 로그에만 429가 남고 llm-gateway 자체 로그는 200이어서(HTTPChaos가 응답을 바꾼다) 보고서가 "응답을 바꾼 곳"을 확정하지 못하고 호출자 쪽에 머무른다.

- `w4-final` C1 회차 1: "1순위로 확인된 실패 동작은 Deployment/agent가 llm-gateway 응답을 429 insufficient_quota로 기록한 뒤 오류를 HTTP 200의 data.answer로 감싸 반환하여 채팅 시나리오를 실패시키는 것이며, 외부 할당량 초과라는 근본 원인은 상충 로그로 판단 보류입니다."
- `w5-final-kpi-f97c068` C1 회차 1: "1순위 원인 후보는 Deployment/agent가 llm-gateway-svc 호출에서 받은 429 insufficient_quota를 오류 답변으로 감싸 HTTP 200으로 전달하는 구간이며, 조직 1의 채팅이 실패하지만 실제 429 생성 위치와 할당량 소진 여부는 확정하지 못했습니다."

### 4.2 원인 리소스 필드 비어 있음 (C1)

8회. 결론에는 llm-gateway-svc의 429가 적혀 있으나 `cause_resource`가 비어 있어 엄격 기준에서 실패한다.

- `w4-retest-20261103-cb91f5d` C1 회차 1: "1순위 조사 대상은 LLM Gateway 응답 전달 구간입니다. Pod/agent-7bfc469fb6-tzz5s가 하위 Service/llm-gateway-svc의 429·insufficient_quota를 받아 채팅 답변을 오류로 반환했지만, 서버는 해당 agent IP에 최종 200을 기록했으므로 실제 오류 생성 리소스와 잘못된 동작은 확정하지 못했습니다."
- `w5-75-tuned-c458dd6-r3` C1 회차 1: "원인 리소스는 미확정이며, agent Pod가 llm-gateway-svc 호출에서 받은 429·insufficient_quota를 채팅 오류로 전달했지만 llm-gateway의 같은 시각 기록은 200이어서 실제 할당량 소진과 오류 생성 지점은 판단 보류입니다."

### 4.3 경로 Service 지목 (C1 llm-gateway-svc)

3회. 429 수신 구간인 Service/llm-gateway-svc를 지목했다. 정답 파일의 Service 이름(llm-gateway)과 달라 엄격 기준에서 실패하지만 llm-gateway로 가는 경로 자체이므로 원인 지점 기준으로는 정답이다. w5-246 계열 배치에서만 나왔다.

- `w5-246-base` C1 회차 2: "1순위 조사 대상은 Service/llm-gateway-svc에서 agent가 수신한 429 insufficient_quota 응답으로 채팅 답변 생성이 실패하는 경로이며, 해당 Service를 통한 오류 전달은 확인했지만 실제 오류 생성 지점과 할당량 소진은 확정하지 못했습니다."

### 4.4 하위 의존성 미규명 (C2 admin까지만)

24회. 로그인 호출이 admin에서 시간 초과된다는 점까지 찾고 admin이 redis를 기다리는지, 네트워크가 막힌 것인지는 "미확정"으로 남겼다. 정답 `root_cause`가 admin→redis 차단이므로 엄격 기준에서 실패한다. redis를 지목해 엄격 기준을 통과한 회차는 3회(w5-75-tuned 2회, w5-246-base 1회)뿐이다. cb91f5d 배치의 3회는 readiness(/readyz) 시간 초과를 앞세워 서술한 경우로, 이 중 하나도 redis 연결 문제를 말하지 않았다.

- `w4-final` C2 회차 2: "원인 1순위는 추정상 Deployment/admin의 로그인 처리 중 응답 대기이며, gateway가 admin:8001의 응답을 받지 못해 로그인과 채팅 진입이 실패하지만 정확한 대기 원인은 확정하지 못했습니다."
- `w5-246-edge2` C2 회차 2: "추정: Pod/admin-7c86f56974-qct64의 HTTP 요청 처리 정체가 1순위 원인 후보이며 로그인 시간 초과로 채팅 경로가 중단되지만, DB·캐시 응답 대기와 공통 네트워크 차단을 구분하지 못해 원인은 확정하지 못했습니다."

### 4.5 원인 미발견 (B1)

2회. w4-final의 B1에서 agent를 조사하고도 Pod별 500 응답을 확인하지 못해 1순위를 미확정으로 남겼다. 이후 Pod별 직접 호출(w4-hardening-m3부터)이 들어간 뒤에는 cb91f5d의 1회(4.6 참고)를 빼고 원인 Pod를 맞혔다.

- `w4-final` B1 회차 1: "1순위 원인은 미확정이며, 추가 조사 대상인 Deployment/agent에서 잘못된 설정이나 특정 Pod의 실패 동작을 확인하지 못해 제보된 간헐 채팅 실패를 이 리소스의 문제로 단정할 수 없습니다."
- `w4-final` B1 회차 2: "원인 1순위는 확정하지 못했습니다. Deployment/agent는 직접 조회했으나 잘못된 설정이나 실패 동작은 확인되지 않았고, 사전 채팅은 성공해 사용자 제보의 간헐 실패 원인과 영향 범위는 판단 보류입니다."

### 4.6 잘못된 원인 (평가 설정·무관 서비스·다른 신호)

3회. 원인 지점 기준에서도 오답인 회차로, 3가지 양상이다. (1) w4-final B1 round 0은 gateway Pod 교체를 지목했다. (2) cb91f5d B1 round 0은 agent Pod를 지목했지만 근거가 429 응답이라 B1 신호(500)와 다르다. (3) f97c068-c2-tcp C2 round 1은 readiness 검사 HTTP→TCP 변경을 원인으로 돌렸다.

- `w4-final` B1 회차 0: "추정: 1순위 원인 후보는 Deployment/gateway의 반복적인 Pod 교체 중 요청 처리 단절이며, 이로 인해 일부 채팅 요청이 실패했을 가능성이 있으나 실패 시각·로그와의 상관관계는 확인하지 못했습니다.[ev-0015][ev-0019]"
- `w5-final-kpi-f97c068-c2-tcp` C2 회차 1: "1순위는 Deployment/admin의 준비 상태 검사가 HTTP /readyz에서 TCP 8001로 변경되어 HTTP 응답 불능 Pod를 Ready와 트래픽 대상으로 유지하는 설정이며, 로그인 시간 초과로 채팅 첫 단계가 차단된 상태에서 응답 불능의 근본 원인은 미확정입니다."

### 시나리오별 정리

| 시나리오 | 엄격 실패 | 주된 유형 | 원인 지점 오답 |
|---|---|---|---|
| B1 | 4/18 | 잘못된 원인 2, 원인 미발견(B1) 2 | 4 |
| C1 | 33/42 | 호출자·전달자 지목(C1 agent) 22, 원인 리소스 필드 비어 있음(C1) 8, 경로 Service 지목(C1 llm-gateway-svc) 3 | 0 |
| C2 | 25/28 | 하위 의존성 미규명(C2 admin까지만) 24, 잘못된 원인 1 | 1 |

A1·A3·A5·C7은 엄격 실패가 없다(모두 원인 리소스까지 일치).

## 5. 엄격 실패 회차 목록

| batch | history | 시나리오 | 회차 | 지목 리소스 | 원인 지점 판정 | 유형 | 한 줄 요약 |
|---|---|---|---|---|---|---|---|
| w4-final | 20261102T114138Z-B1 | B1 | 0 | Deployment/gateway | 오답 | 잘못된 원인 | gateway Pod 교체를 원인으로 돌렸다. |
| w4-final | 20261102T114138Z-B1 | B1 | 1 | (비어 있음) | 오답 | 원인 미발견(B1) | agent에서 이상을 확인하지 못해 원인 Pod를 찾지 못했다. |
| w4-final | 20261102T114138Z-B1 | B1 | 2 | (비어 있음) | 오답 | 원인 미발견(B1) | agent에서 이상을 확인하지 못해 원인 Pod를 찾지 못했다. |
| w4-final | 20261102T115027Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-final | 20261102T115732Z-C2 | C2 | 0 | Pod/admin-587f85bc8f-twsvp | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-final | 20261102T115732Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-final | 20261102T115732Z-C2 | C2 | 2 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-hardening-m3 | 20261102T145403Z-C1 | C1 | 0 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-hardening-m3 | 20261102T145403Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-hardening-m3 | 20261102T145403Z-C1 | C1 | 2 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | llm-gateway 응답 경로를 조사 대상으로 쓰되 원인 리소스는 비웠다. |
| w4-hardening-m3 | 20261102T150440Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-hardening-m3 | 20261102T150440Z-C2 | C2 | 2 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-hardening-m4 | 20261102T154106Z-C1 | C1 | 0 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-hardening-m4 | 20261102T154106Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-hardening-m4 | 20261102T154106Z-C1 | C1 | 2 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-hardening-m4 | 20261102T155140Z-C2 | C2 | 0 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-hardening-m4 | 20261102T155140Z-C2 | C2 | 1 | Pod/admin-587f85bc8f-twsvp | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-hardening-m4 | 20261102T155140Z-C2 | C2 | 2 | Pod/admin-587f85bc8f-twsvp | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w4-c1-tools-vs-recheck | 20261102T165058Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-c1-tools-vs-recheck | 20261102T165058Z-C1 | C1 | 2 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-c1-tools-vs-recheck | 20261102T165058Z-C1 | C1 | 3 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-c1-tools-vs-recheck | 20261102T165058Z-C1 | C1 | 4 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-c1-tools-vs-recheck | 20261102T165058Z-C1 | C1 | 5 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-cause-chain-146 | 20261102T171404Z-C1 | C1 | 1 | Pod/agent-79d54f845b-w8pnh | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-cause-chain-146 | 20261102T171404Z-C1 | C1 | 2 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-retest-20261103-cb91f5d | 20261102T193722Z-B1 | B1 | 0 | Pod/agent-5756c4bc97-4n9mg | 오답 | 잘못된 원인 | agent Pod를 지목했으나 근거가 429 응답이라 B1 신호와 다르다. |
| w4-retest-20261103-cb91f5d | 20261102T194656Z-C1 | C1 | 0 | Pod/agent-7bfc469fb6-plfxr | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-retest-20261103-cb91f5d | 20261102T194656Z-C1 | C1 | 1 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w4-retest-20261103-cb91f5d | 20261102T194656Z-C1 | C1 | 2 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w4-retest-20261103-cb91f5d | 20261102T203058Z-C2 | C2 | 0 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | readiness(/readyz) 시간 초과를 앞세워 서술하고 내부 원인은 미확정으로 남겼다. |
| w4-retest-20261103-cb91f5d | 20261102T203058Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | readiness(/readyz) 시간 초과를 앞세워 서술하고 내부 원인은 미확정으로 남겼다. |
| w4-retest-20261103-cb91f5d | 20261102T203058Z-C2 | C2 | 2 | Pod/admin-59dd5c6496-b6lzm | 정답 | 하위 의존성 미규명(C2 admin까지만) | readiness(/readyz) 시간 초과를 앞세워 서술하고 내부 원인은 미확정으로 남겼다. |
| w5-c1-recheck | 20261109T052103Z-C1 | C1 | 0 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-75-base-c458dd6-r3 | 20261109T144246Z-C1 | C1 | 0 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-75-base-c458dd6-r3 | 20261109T144246Z-C1 | C1 | 1 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-75-tuned-c458dd6-r3 | 20261109T145348Z-C1 | C1 | 0 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-75-tuned-c458dd6-r3 | 20261109T145348Z-C1 | C1 | 1 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-75-tuned-c458dd6-r3 | 20261109T145348Z-C1 | C1 | 2 | Pod/agent-7bfc469fb6-plfxr | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-75-base-c458dd6-c2-tcp | 20261109T150442Z-C2 | C2 | 1 | Pod/admin-7c86f56974-wdtbt | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-75-base-c458dd6-c2-tcp | 20261109T150442Z-C2 | C2 | 2 | Pod/admin-7c86f56974-wdtbt | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-75-base-c458dd6-c2-tcp-isolated | 20261109T151856Z-C2 | C2 | 0 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-75-base-c458dd6-c2-tcp-isolated | 20261109T151856Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-75-base-c458dd6-c2-tcp-isolated | 20261109T151856Z-C2 | C2 | 2 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-75-tuned-c458dd6-c2-tcp-isolated | 20261109T153156Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-final-kpi-f97c068 | 20261109T171719Z-C1 | C1 | 0 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-final-kpi-f97c068 | 20261109T171719Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-final-kpi-f97c068-c2-tcp | 20261109T173402Z-C2 | C2 | 0 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-final-kpi-f97c068-c2-tcp | 20261109T173402Z-C2 | C2 | 1 | Deployment/admin | 오답 | 잘못된 원인 | readiness 검사의 HTTP→TCP 변경을 1순위로 지목했다. |
| w5-final-kpi-f97c068-c2-tcp | 20261109T173402Z-C2 | C2 | 2 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-246-base | 20261109T190006Z-C1 | C1 | 0 | Pod/agent-969b56679-2gv6k | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-246-base | 20261109T190006Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-246-base | 20261109T190006Z-C1 | C1 | 2 | Service/llm-gateway-svc | 정답 | 경로 Service 지목(C1 llm-gateway-svc) | llm-gateway-svc 호출 구간의 429를 짚었으나 정답 이름(llm-gateway)과 달리 Service 이름으로 지목했다. |
| w5-246-edge | 20261109T191907Z-C1 | C1 | 0 | Pod/agent-969b56679-2gv6k | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-246-edge | 20261109T191907Z-C1 | C1 | 1 | Deployment/agent | 정답 | 호출자·전달자 지목(C1 agent) | llm-gateway의 429를 전달한 agent를 1순위로 지목했다. |
| w5-246-edge | 20261109T191907Z-C1 | C1 | 2 | Service/llm-gateway-svc | 정답 | 경로 Service 지목(C1 llm-gateway-svc) | llm-gateway-svc 호출 구간의 429를 짚었으나 정답 이름(llm-gateway)과 달리 Service 이름으로 지목했다. |
| w5-246-edge2 | 20261109T193443Z-C1 | C1 | 0 | (비어 있음) | 정답 | 원인 리소스 필드 비어 있음(C1) | 429 수신 경로는 짚었으나 원인 리소스를 비워 두었다. |
| w5-246-edge2 | 20261109T193443Z-C1 | C1 | 2 | Service/llm-gateway-svc | 정답 | 경로 Service 지목(C1 llm-gateway-svc) | llm-gateway-svc 호출 구간의 429를 짚었으나 정답 이름(llm-gateway)과 달리 Service 이름으로 지목했다. |
| w5-246-edge2 | 20261109T194513Z-C2 | C2 | 0 | Pod/admin-7c86f56974-qct64 | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-246-edge2 | 20261109T194513Z-C2 | C2 | 1 | Pod/admin-7c86f56974-qct64 | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-246-edge2 | 20261109T194513Z-C2 | C2 | 2 | Pod/admin-7c86f56974-qct64 | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-246-base | 20261109T200115Z-C2 | C2 | 1 | Deployment/admin | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |
| w5-246-base | 20261109T200115Z-C2 | C2 | 2 | Pod/admin-7c86f56974-qct64 | 정답 | 하위 의존성 미규명(C2 admin까지만) | admin 응답 정지·시간 초과까지 찾고 redis 연결 등 내부 원인은 미확정으로 남겼다. |

## 6. 다음 단계 시사점

1. C1 엄격 실패 33회는 전부 원인 지점 기준으로 정답이다. 모든 회차가 agent 로그의 429 수신을 근거로 들었고, 429를 "만든" 리소스는 llm-gateway 로그(200)와 충돌해 확정하지 못했다. 다음 단계는 호출 체인에서 응답 변조 지점(llm-gateway 앞단)을 가리는 근거를 모으는 일이다.
2. 지목 리소스가 회차마다 달라진다(Deployment/agent, Pod/agent, Service/llm-gateway-svc, 비어 있음). 같은 신호를 두고도 표기가 갈려 엄격 채점이 흔들리므로, 원인 후보를 Deployment 단위로 정규화하는 규칙이 필요하다.
3. C2는 admin의 응답 정지까지는 안정적으로 찾지만(원인 지점 기준 오답은 readiness 설정 지목 1회뿐) 그 안쪽인 redis 연결은 3회만 짚었다. admin 로그에 redis 줄이 남지 않는다는 `expected.yaml`의 도구 한계 설명과 같은 방향이며, admin→redis 직접 연결 확인(하위 의존성 probe)이 다음 과제다.
4. 평가용 설정 변경(readiness HTTP→TCP)이 있는 배치에서 원인을 설정 변경으로 돌린 오답이 1회 나왔다. 평가 조건이 보고서에 드러나 원인 해석을 흐릴 수 있으므로, 평가용 변경과 장애 원인을 구분해 보여 주는 장치가 필요하다.
5. B1은 Pod별 직접 호출이 들어간 w4-hardening-m3 이후 15회 중 14회 원인 Pod를 맞혔고, 오답은 cb91f5d 1회였다(w4-final 3회는 그 이전). cb91f5d 오답은 B1 중에 C1과 같은 429 신호를 끌어온 사례여서, 한 시나리오에서 다른 시나리오의 신호가 섞이는 경우를 별도로 점검할 필요가 있다.
