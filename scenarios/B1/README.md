# B1 — agent Pod 1개만 HTTP 500

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `KG_CHAOS_NS`(Chaos 객체를 만들 네임스페이스, 기본 `chaos-mesh`), `TEST_ENV`(테스트 사용자 파일, 기본 `.env.test-user`), `B1_REQUESTS`(front-chat 경유 요청 수, 기본 36), `B1_CONCURRENCY`(동시 요청 수, 기본 12).
전제: agent replicas 3 (로컬 overlay), `make seed`.

## 주입 내용

Chaos Mesh HTTPChaos(`mode: one`, `target: Response`, `port: 8006`, `path: "*"`, `replace.code: 500`)로 agent Pod 하나를 골라 그 Pod의 모든 HTTP 응답 코드를 500으로 바꾼다. 어느 Pod가 골렸는지는 inject 출력과 HTTPChaos `status.experiment.containerRecords`에 나온다. 객체는 `chaos-mesh/HTTPChaos/kg-b1-<네임스페이스>`로, 진단 대상 네임스페이스 밖에 만든다.

- Chaos Mesh는 대상 Pod 네임스페이스(`dev`)에 PodHttpChaos 객체를 만든다. 원복 뒤에도 spec이 빈 채로 남는다 (Chaos Mesh 동작). KubeGuardian은 이 kind를 수집하지 않는다.
- 주입·원복 모두 Pod를 재시작하지 않는다 (restartCount 0 유지 확인).

## 기대 증상

- agent Pod를 Pod IP로 직접 부르면 1개만 500, 나머지 2개는 200이다. agent Pod 3개는 모두 Ready다 (readinessProbe가 tcpSocket).
- front-chat 경유 agent 요청의 **일부**가 500으로 실패한다. 기획서의 "약 1/3"과 달리 비율이 실행마다 크게 다르다 (36번 중 1~16번). gateway가 keep-alive 연결을 재사용하고 kube-proxy는 연결 단위로 Pod를 고르기 때문이다. 순차 요청은 한 연결에 몰려 전부 성공하거나 전부 실패했고(각각 확인), 동시 요청이어야 연결이 여러 개 열려 Pod가 나뉜다. 그래서 verify_symptom은 동시 요청으로 확인한다.
- 같은 이유로 H0 스모크(순차 요청)는 B1 주입 중에도 통과하거나 실패할 수 있다.
- 규칙 점검: 장애 신호 없음, 경로 C. 기대 경로 B는 사용자 입력(증상 신고)이 연결된 뒤에 나온다.

## 확인 상태

2026-10-19 템플릿 포크(`dev`)에서 확인했다.

- inject → AllInjected → verify_symptom 통과 4회 (Pod별 `200 500 200` 등, front-chat 경유 36번 중 1·2·3·16번 실패). 같은 상태에서 `kg check`는 장애 신호 없음, 경로 C.
- reset → HTTPChaos 삭제, verify_symptom 실패(Pod별 `200 200 200` = 증상 없음), H0 스모크 성공.
- `make eval S=B1`: 실패(경로 C, 기대 B). 사용자 입력 경로가 아직 없어 예상된 결과다. 원복·H0 복구는 성공, 전체 15초.
