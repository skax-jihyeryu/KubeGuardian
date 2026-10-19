# A3 — 신규 배포가 없는 이미지 tag를 가리킴

| 파일 | 역할 |
|---|---|
| `expected.yaml` | 정답 원인, 영향 서비스, 기대 1순위, `k8s_visible` |
| `inject.sh` | 장애 주입 |
| `verify_symptom.sh` | 증상 발생 확인. 실패하면 그 실행은 무효 |
| `reset.sh` | 원복 |

환경 변수: `KG_NAMESPACE`(기본 `dev`), `A3_DEPLOY`(기본 `agent`), `A3_CONTAINER`(기본 Deployment 이름), `A3_IMAGE`(기본 `registry.invalid/kg-target/<컨테이너>:1.4.2`), `A3_POD_SELECTOR`(기본 `app=<Deployment>`).

## 주입 내용

agent Deployment의 컨테이너 이미지를 받을 수 없는 이미지(`registry.invalid/kg-target/agent:1.4.2`)로 바꾸고 `imagePullPolicy`를 `IfNotPresent`로 바꾼다. 원래 이미지·`imagePullPolicy`는 `scenarios/A3/.state/<네임스페이스>-<Deployment>.image`(커밋하지 않음)에 저장하고 `reset.sh`가 되돌린다.

- 이미지 이름: tag는 그럴듯한 버전(`1.4.2`)으로 둔다. 이름에 정답이 드러나면 조사 에이전트가 이름만 보고 맞힐 수 있다. 레지스트리는 해석되지 않는 이름(`.invalid`, RFC 2606)이다. 레지스트리를 생략하면 docker.io에서 받으므로, 누가 그 이름을 올리면 이미지가 받아져 버린다.
- 그래서 오류 메시지는 "저장소 없음"이 아니라 레지스트리 DNS 조회 실패(`dial tcp: lookup registry.invalid ... no such host` 형태)다. 대기 사유는 똑같이 `ErrImagePull` → `ImagePullBackOff`다. 실제 장애의 "없는 tag"(레지스트리가 `manifest unknown`을 돌려줌)와 메시지가 다르다는 점은 조사 결과를 채점할 때 감안한다.
- `imagePullPolicy`도 바꾸는 이유: 로컬 이미지는 `minikube` 노드에 직접 빌드해 `Never`로 쓴다. `Never`인 채로 tag만 바꾸면 대기 사유가 `ErrImageNeverPull`이 되는데, 실제 클러스터에서 받을 수 없는 이미지는 레지스트리에서 받다가 실패한다(`ErrImagePull` → `ImagePullBackOff`). 규칙 P03도 이 두 사유를 본다.
- 원복하면 Pod 템플릿이 원래와 같아져 Deployment가 이전 ReplicaSet을 다시 쓴다. 기존 Pod는 재시작되지 않는다. 롤아웃이 끝나면 주입 이미지를 가진 ReplicaSet(replicas 0)을 지운다. 남겨 두면 다음 시나리오에서 이전 주입의 흔적이 보이기 때문이다.

## 기대 증상

- 새 ReplicaSet의 Pod 1개가 `ErrImagePull`·`ImagePullBackOff`로 대기한다. 롤링 업데이트의 `maxUnavailable`(25%)이 replicas 3에서 0으로 내림돼 이전 Pod 3개는 Ready로 남는다. 사용자 기능(로그인·채팅)은 정상이다.
- `progressDeadlineSeconds`(600초)가 지나면 Deployment `Progressing=False`(`ProgressDeadlineExceeded`)가 된다.
- 규칙 점검: W02가 주입 후 약 25~31분에 적발(🟠 주의, 이전 버전이 서비스 중), P03은 약 1시간 뒤 적발. W01은 적발되지 않는다(`availableReplicas = replicas`, 05-rules §5.1). 게이트 경로 A.
- `make eval S=A3`는 증상 확인 직후 점검을 한 번만 하므로 W02·P03이 지속 시간 전이다. 결과는 실패(경로 C, P03·S02 관찰 중)로 나온다. 지속 시간을 채우는 반복 점검은 러너의 후속 과제다 (아래 확인 상태).

## 확인 상태

2026-10-19 템플릿 포크(`dev`)에서 확인했다. 아래 시간대별 점검과 `make eval`은 처음 이미지(`kg-target/agent:<정답이 드러나는 tag>`, docker.io에서 "저장소 없음")로 했다. 리뷰 반영 후 지금 이미지로 inject → verify 통과(대기 메시지 `lookup registry.invalid ... no such host`) → reset(주입 ReplicaSet 삭제, agent ReplicaSet 1개만 남음) → H0 스모크 성공을 다시 확인했다. 대기 사유가 같으므로 규칙 판정은 같다.

- inject(12:01:16 UTC) → 10초 안에 verify_symptom 통과 (`ErrImagePull` Pod 1개, 이전 버전 Ready 3/3).
- 같은 점검 DB로 5분마다 `kg check`:

  | 점검 시각 | 주입 후 | P03 | S02 | W02 | 게이트 |
  |---|---|---|---|---|---|
  | 12:06:52 | 6분 | 관찰 중 | 관찰 중 | 통과 | C |
  | 12:11:54 | 11분 | 관찰 중 | 관찰 중 | 관찰 중 (`ProgressDeadlineExceeded`) | C |
  | 12:16:56 | 16분 | 관찰 중 | **적발**(주의) | 관찰 중 | A (warning) |
  | 12:27:01 | 26분 | 관찰 중 | 적발 | **적발**(주의) | A (warning) |

  W01은 계속 통과였다 (05-rules §5.1대로). P03은 1시간을 기다리지 않았다.
- **기대 밖 신호 S02**: 이미지를 못 받는 새 Pod도 Service selector에 맞아 EndpointSlice에 Ready가 아닌 엔드포인트로 잡힌다. 그래서 S02 "일부"가 15분 규칙으로 W02보다 먼저(약 16분) 적발됐다. 05-rules §5의 A3 행에는 S02가 없다. 기대 신호에 넣을지, S02에서 "롤아웃 중인 새 ReplicaSet의 Pod"를 제외할지는 명세 결정이다 (후속 후보).
- reset → 롤아웃 완료, 새 ReplicaSet 0개, verify_symptom 실패(증상 없음), H0 스모크 성공.
- `make eval S=A3`: 실패(경로 C, 놓침 W02, 관찰 중 P03·S02). 러너가 증상 확인 직후 한 번만 점검하기 때문이다. 원복·H0 복구는 성공.
