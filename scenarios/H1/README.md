# H1 — 설정 없이도 정상인 서비스 (부재 정상 기준선)

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 (장애 신호 전부 금지, 원인이 아닌 것) |
| `manifest.yaml` | h1-noconfig Deployment·Service |
| `inject.sh` | 서비스 추가 |
| `verify_symptom.sh` | Ready Pod 1개, 엔드포인트 1개, H0 스모크 성공 |
| `reset.sh` | 삭제 후 `h1-` 리소스가 사라질 때까지 대기 (최대 60초) |

환경 변수: `KG_NAMESPACE`(기본 `dev`).

## 주입 내용

`dev`에 `h1-noconfig`(replicas 1)와 Service를 추가한다. 노드에 있는 이미지만 쓴다(`imagePullPolicy: Never`).

- 컨테이너 `web` = `nginx:1.27-alpine`(포트 80, readinessProbe httpGet `/`)
- 컨테이너 `client` = `busybox:1.36`. env `CACHE_URL=http://localhost:80`(같은 Pod `web` 사이드카)과 `LOG_LEVEL=info`를 Deployment에 직접 적는다
- ConfigMap·Secret 참조와 `*_SERVICE_URL`은 없다. requests·limits는 모두 지정한다

## 기대

설정이 없고 localhost 주소가 있어도 장애가 아니다. 규칙은 장애 신호를 하나도 내면 안 된다(S04 사이드카 예외, C05는 선언 없음). 경로 C가 기대이고 `forbidden_signals: ["*"]`로 채점한다. 개선 권고(H)는 나올 수 있다.
