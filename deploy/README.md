# deploy

쿠버네티스 매니페스트.

| 폴더 | 내용 |
|---|---|
| `kubeguardian/` | SA·Role(dev)·ClusterRole(Node 조회만), runner Deployment, (W3) PVC, CronJob, diagnostic-agent Deployment |
| `chaos-mesh/` | 평가 환경 전용 장애 주입 도구 |

## runner Pod (`kubeguardian/runner.yaml`)

KubeGuardian을 클러스터 안에서 실행하는 Pod다. 클러스터 주소(`<svc>.dev.svc`)로 부르는 증상 감지와 AI의 HTTP 도구가 여기서만 동작한다. 평가 러너가 `kubectl exec`로 점검·조사를 시킨다 (`eval/README.md` "클러스터 안 실행").

| 명령 | 하는 일 |
|---|---|
| `make kg-up` | 이미지를 minikube 노드에 빌드(`kubeguardian:local`) → 네임스페이스·RBAC(조회 전용) → Secret `kubeguardian-env`(`.env`의 LLM 키 4개만, 값 비출력) → ConfigMap `kubeguardian-config`(`kubeguardian.yaml`, 없으면 예시)·`kubeguardian-ca`(사내 CA 번들) → runner 기동·Ready 대기 |
| `make kg-down` | runner·Secret·ConfigMap 삭제 (RBAC·Phoenix는 남김) |

- runner는 `sleep infinity`로 떠 있고 점검 DB는 emptyDir(`/data`)다. Pod가 다시 뜨면 DB가 사라진다.
- 추적은 클러스터 안 주소 `phoenix.kubeguardian.svc`로 보낸다 (port-forward 불필요).
- 설정(`kubeguardian.yaml`)이나 코드를 바꾸면 `make kg-up`을 다시 한다 (runner를 재시작한다).
- Secret `kg-test-user`(증상 시나리오 login 계정)는 `/etc/kubeguardian/accounts/kg-test-user`에 읽기 전용으로 마운트한다 (`optional`이라 없어도 Pod는 뜬다). `make kg-up`은 이 Secret을 만들지 않는다.
  - kubeguardian 네임스페이스에 Secret `kg-test-user`(키 `id`, `password`)를 사용자가 직접 만든다. 값은 채팅·로그에 남기지 않는다.
  - client-side `kubectl apply`는 값을 `last-applied-configuration` annotation에 남기므로 쓰지 않는다. `kubectl create secret generic ... --from-file`로 만들거나 `kubectl apply --server-side`를 쓴다.
  - `make kg-down`은 이 Secret을 지우지 않는다.

## 주기 점검 CronJob (`kubeguardian/cronjob.yaml`)

5분마다 `cli.py check --trigger schedule`을 실행해 규칙 점검과 증상 감지 결과를 PVC `kubeguardian-data`의 SQLite(`/data/kubeguardian.db`)에 쌓는다 (`check_run.trigger = schedule`). `make kg-up`이 함께 적용한다.

- `concurrencyPolicy: Forbid`: 이전 점검이 끝나지 않았으면 다음 회차를 건너뛴다. `activeDeadlineSeconds: 270`으로 다음 회차 전에 끝내고, `backoffLimit: 0`이라 실패해도 재시도하지 않는다.
- AI 조사는 포함하지 않는다. 사건 지문(#26)이 없어 같은 장애를 5분마다 다시 조사하기 때문이다. #26 뒤에 `investigate`로 바꾼다.
- PVC는 RWO(단일 노드 minikube 전제)다. runner의 emptyDir DB와 분리돼 평가 이력과 섞이지 않는다.
- 평가 중에는 `make kg-cron-suspend`로 멈추고 `make kg-cron-resume`으로 재개한다. `make kg-cron-run`은 지금 1회 실행한다.

