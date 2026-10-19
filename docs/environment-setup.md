# KubeGuardian AI — 로컬 환경 설치 가이드 (minikube)

> 실행 계획서 [02-execution-plan.md](02-execution-plan.md) W1의 클러스터·Chaos Mesh 부분을 단계별로 실행하고 확인하기 위한 문서.
> 각 단계는 **왜 필요한가 → 실행 → 확인 → 실패 시** 순서로 적었다. 확인 기준을 만족해야 다음 단계로 넘어간다.

## 진행 체크리스트

| 단계 | 내용 | 확인 기준 | 완료 |
|---|---|---|---|
| 0 | 도구 설치 확인 | 버전 출력 | ✅ |
| 1 | Docker Desktop 실행·자원 설정 | `docker info`에 메모리 ≥ 12GB | ✅ (10-13, 14 CPU · 약 11.7GiB) |
| 2 | 사내 CA 인증서 추출·등록 | `~/.minikube/certs/sk-ca.pem` 존재 | ✅ |
| 3 | minikube 클러스터 시작 | 노드 Ready, calico Pod Running | ✅ (10-13, K8s v1.37.0, docker://29.7.2) |
| 4 | 노드 내부 CA 동작 확인 | registry.k8s.io 이미지 pull 성공 | ✅ (pull 성공, 접속 401) |
| 5 | metrics-server | `kubectl top nodes` 값 출력 | ✅ |
| 6 | 네임스페이스 | dev·kubeguardian·chaos-mesh 존재 | ✅ |
| 7 | Chaos Mesh 설치 | chaos-mesh Pod 전부 Running | ✅ (차트 2.8.4) |
| 8 | Chaos Mesh 스모크 테스트 (4종) | 4종 모두 효과 관측 | ⚠️ 3종 성공, TimeChaos 실패 (아래 결과 기록) |
| 9 | Pod 안 진단용 Azure OpenAI 호출 | `run.sh` `OK` 출력 | ✅ (10-19, gpt-6.1-sol) |
| 10 | 진단 대상 기동과 정상 기준선 | `make smoke` `OK` 출력 | ✅ (10-19, gpt-4.1) |

## 이 환경의 전제 (2026-10-02 확인)

| 항목 | 값 |
|---|---|
| 머신 | Apple Silicon (arm64), 14코어, 메모리 24GB |
| 도구 | minikube v1.39.0, kubectl v1.37.1, helm v4.3.0, k6 v2.3.0, docker CLI 29.4.3 |
| 사내 네트워크 | **TLS 가로채기 프록시 있음** (아래 표) |

**사내 프록시가 가로채는 도메인** — 이 도메인들은 회사 인증서 `SK holdings C&C`로 다시 서명되어 도착한다.

| 도메인 | 가로챔 | 이 프로젝트에서 받는 것 |
|---|---|---|
| registry.k8s.io | 🔴 | metrics-server 이미지 (`registry.k8s.io/metrics-server/metrics-server:v0.9.0`), K8s 시스템 이미지 |
| quay.io | 🔴 | **calico CNI 이미지** (`quay.io/calico/{cni,node,kube-controllers}:v3.32.2`) |
| gcr.io | 🔴 | minikube 노드 베이스 이미지(kicbase) — Mac의 Docker Desktop이 받음 |
| charts.chaos-mesh.org | 🔴 | Chaos Mesh helm 차트 — Mac의 helm이 받음 |
| docker.io, ghcr.io, pypi, npm, github | 🟢 | 앱 베이스 이미지, Chaos Mesh 이미지, 패키지 |
| `<리소스명>.openai.azure.com` | ❓ | 리소스명 확정 후 확인 (다음 단계 문서) |

확인 방법 (도메인 하나 점검):

```bash
echo | openssl s_client -connect registry.k8s.io:443 -servername registry.k8s.io 2>/dev/null | openssl x509 -noout -issuer
```

`CN=SK holdings C&C`가 나오면 가로채는 도메인이다.

---

## 0. 도구 설치 확인 ✅

| 도구 | 왜 필요한가 |
|---|---|
| **docker** (Docker Desktop) | minikube 노드를 실행하는 기반, 앱 이미지 빌드 (→ 1단계에서 자세히) |
| **minikube** | 로컬에 단일 노드 Kubernetes 클러스터를 만든다. 진단 대상(템플릿 앱)과 진단 에이전트가 모두 여기서 돈다 |
| **kubectl** | 클러스터에 리소스를 배포하고 상태를 본다. 장애 주입(Service·ConfigMap 수정)도 kubectl로 한다 |
| **helm** | Chaos Mesh를 설치한다. Chaos Mesh는 CRD·컨트롤러·DaemonSet 묶음이라 helm 차트로 설치하는 것이 표준 |
| **k6** | 부하 발생. 부하가 있어야 나타나는 장애(D1 CPU 제한 → 504 연쇄)에 쓴다. 이 문서 범위에서는 쓰지 않고 W4부터 사용 |

```bash
minikube version && kubectl version --client && helm version --short && k6 version && docker version --format '{{.Client.Version}}'

minikube version: v1.39.0
commit: 7a9f6a841470a207de8cf4bafcccee0969d8ba10
Client Version: v1.34.1
Kustomize Version: v5.7.1
v4.3.0+gbec5b06
k6 v2.3.0 (commit/devel, go1.27.1, darwin/arm64)
29.4.3
```

---

## 1. Docker Desktop 실행·자원 설정

### 왜 필요한가

**① Mac에서는 컨테이너를 직접 실행할 수 없다.**
컨테이너는 Linux 커널 기능(namespace, cgroup)으로 만들어진다. macOS 커널에는 이 기능이 없어서, Docker Desktop이 **경량 Linux VM**을 띄우고 그 안에서 컨테이너를 실행한다. Mac의 `docker` 명령은 이 VM 안의 Docker 데몬에 요청을 보내는 클라이언트일 뿐이다. 데몬이 꺼져 있으면 `Cannot connect to the Docker daemon` 오류가 난다.

**② minikube의 노드 자체가 Docker 컨테이너다.**
`--driver=docker`를 쓰면 minikube는 Kubernetes 노드 하나를 **Docker 컨테이너 한 개**(kicbase 이미지)로 만든다. 그 컨테이너 안에서 kubelet, API 서버, 컨테이너 런타임이 돌고, 우리 앱 Pod는 그 안에서 다시 컨테이너로 뜬다.

```text
macOS
└─ Docker Desktop Linux VM        ← 메모리·CPU 상한은 여기서 정해짐
   └─ 컨테이너 "minikube" (= K8s 노드, --memory=10g)
      ├─ kube-apiserver, kubelet, coredns, calico ...
      ├─ dev 네임스페이스: gateway, agent ×3, ... (앱 Pod)
      └─ chaos-mesh: chaos-daemon (노드의 런타임 소켓에 접근해 장애 주입)
```

**③ 자원 상한이 이중으로 걸린다.**
minikube에 준 `--memory=10g`는 Docker Desktop VM의 메모리 안에서만 쓸 수 있다. VM이 8GB면 minikube가 시작을 거부하거나 Pod가 축출된다. 그래서 Docker Desktop에 **minikube보다 넉넉한 자원**을 줘야 한다.

**④ 앱 이미지 빌드에도 필요하다.**
템플릿 앱 6개를 `docker build`로 빌드한다. 3단계에서 `--container-runtime=docker`로 시작하면 `eval $(minikube docker-env)`로 **노드 안의 Docker 데몬에 직접 빌드**할 수 있어서 이미지를 옮기는 과정이 없어진다.

**⑤ Chaos Mesh 일부 기능이 이 VM의 커널에 의존한다.**
NetworkChaos(지연·차단)는 커널의 `sch_netem` 모듈을 쓴다. docker 드라이버에서는 그 커널이 Docker Desktop VM의 커널이다. 8단계 스모크 테스트에서 실제 동작을 확인한다.

### 실행

1. `/Applications/Docker.app` 실행
2. Settings → Resources → Advanced
   - **CPUs: 6 이상** (minikube 4 + Docker·빌드 여유)
   - **Memory: 12GB 이상** (minikube 10GB + 여유 2GB. 머신 24GB 중 나머지는 macOS·IDE·브라우저용)
   - Disk: 60GB 이상 권장 (앱 이미지 6개 × 버전 2종 + 시스템 이미지)
3. Apply & Restart

### 확인

```bash
docker info --format 'CPUs={{.NCPU}} Mem={{.MemTotal}} Arch={{.Architecture}}'
```

- `CPUs` ≥ 6, `Mem` ≥ 12,000,000,000 정도, `Arch=aarch64`

### 실패 시

- 데몬 연결 오류 → Docker Desktop이 완전히 뜰 때까지(메뉴바 고래 아이콘이 멈출 때까지) 기다린 뒤 재시도
- 메모리 값이 그대로 → Apply & Restart를 눌렀는지 확인

---

## 2. 사내 CA 인증서 추출·등록

### 왜 필요한가

사내 프록시는 일부 HTTPS 연결을 중간에서 풀고 회사 인증서로 다시 서명한다. **그 인증서를 신뢰하는 곳에서만** 연결이 성공한다.

| 실행 위치 | 사내 CA 신뢰 | 결과 |
|---|---|---|
| Mac (brew, helm, kubectl, Docker Desktop) | ✅ IT가 macOS 키체인에 설치해 둠 | 정상. **그래서 도구 설치는 CA 없이도 됐다** |
| **minikube 노드 내부** (이미지 pull) | ❌ 노드는 자체 Linux 인증서 저장소를 씀 | registry.k8s.io·quay.io 이미지가 `x509: certificate signed by unknown authority`로 실패 |
| 컨테이너 내부 (빌드 중 `pip install`, Pod의 외부 호출) | ❌ | 가로채는 도메인이면 실패 (→ 다음 단계 문서에서 Pod용 CA 주입) |

이 프로젝트에서 직접 영향받는 것:

- **calico**(quay.io): `minikube start` 도중 설치된다. CA가 없으면 **클러스터 네트워크가 뜨지 않아 노드가 NotReady**에 머문다. 그래서 CA는 **클러스터를 만들기 전에** 등록해야 한다.
- **metrics-server**(registry.k8s.io): 감지기(N02, D1·D2 추세 센서)와 메트릭 샘플러가 의존한다.

CA 파일을 IT에 따로 요청할 필요는 없다. 키체인에 있는 인증서가 그 자체로 루트(자체 서명)이고 프록시가 보내는 인증서 체인의 최상위와 같다는 것을 확인했다.

| 항목 | 값 |
|---|---|
| 이름 | `SK holdings C&C` (subject = issuer, 루트 CA) |
| 만료 | 2030-04-20 |
| SHA-256 | `E7:C7:A0:90:01:42:B8:9F:F3:54:5F:57:B3:62:71:CE:C8:DF:5F:57:D6:67:30:9A:E7:73:63:DC:C1:2D:AE:16` |

### 실행

```bash
mkdir -p ~/.minikube/certs
```

```bash
security find-certificate -c "SK holdings C&C" -p /Library/Keychains/System.keychain > ~/.minikube/certs/sk-ca.pem
```

minikube는 `~/.minikube/certs/`에 있는 PEM 파일을 `--embed-certs`로 시작할 때 노드에 넣는다 ([minikube 공식 문서](https://minikube.sigs.k8s.io/docs/handbook/untrusted_certs/)).

### 확인

```bash
openssl x509 -in ~/.minikube/certs/sk-ca.pem -noout -subject -fingerprint -sha256
```

- subject에 `CN=SK holdings C&C`, 지문이 위 표와 같으면 성공

### 실패 시

- 파일이 비어 있음 → 키체인 이름 확인: `security find-certificate -a -c "SK" /Library/Keychains/System.keychain | grep labl`
- 나중에 프록시 인증서가 교체되면 이 단계를 다시 하고 4단계로 확인

---

## 3. minikube 클러스터 시작

### 실행

```bash
minikube start --driver=docker --container-runtime=docker --cpus=4 --memory=10g --cni=calico --embed-certs
```

### 옵션별 이유

| 옵션 | 이유 |
|---|---|
| `--driver=docker` | 1단계 설명대로 노드를 Docker 컨테이너로 만든다. Mac에서 가장 설치가 간단하고 빠르다. Chaos Mesh가 이 커널에서 안 되는 기능이 있으면 `--driver=vfkit`(minikube 전용 VM)으로 바꾸는 것이 대안 |
| `--container-runtime=docker` | 노드 안에서 컨테이너를 실행하는 런타임을 **명시적으로 고정**한다. ① `minikube docker-env`로 노드에 직접 이미지 빌드 가능 (B3 "같은 tag에 다른 이미지" 시나리오 재현이 쉬움) ② Chaos Mesh 설치 시 런타임 소켓 값을 확정할 수 있음. 기본값에 맡기면 버전에 따라 달라질 수 있다 |
| `--cpus=4 --memory=10g` | 기획서 §6.1의 자원 예산. 앱 6개(agent ×3, gateway ×2) + postgres·redis + 진단 에이전트 + Phoenix + Chaos Mesh |
| `--cni=calico` | 기본 CNI는 **NetworkPolicy를 적용하지 않는다**. calico는 적용한다. NetworkChaos가 안 될 때 C2(admin↔redis 차단)를 NetworkPolicy로 대체하기 위한 대비이고, K8s 네트워킹 학습 목적에도 맞다 |
| `--embed-certs` | 2단계의 `~/.minikube/certs/*.pem`을 노드에 동기화한다. calico 이미지(quay.io)를 받는 데 바로 필요 |

### 확인

```bash
kubectl get nodes -o wide
```

- STATUS `Ready`, CONTAINER-RUNTIME `docker://...`

```bash
kubectl get pods -n kube-system
```

- `calico-node-*`, `calico-kube-controllers-*`, `coredns-*` 모두 `Running` (첫 기동은 2~3분 걸림)

### 실패 시

| 증상 | 원인 | 조치 |
|---|---|---|
| `Docker Desktop has only NNNNMB memory` | 1단계 자원 부족 | Docker Desktop 메모리 상향 |
| calico Pod `ImagePullBackOff`, describe에 `x509` | CA가 노드에 안 들어감 | 2단계 확인 후 `minikube delete && minikube start ...` (같은 옵션) |
| 노드 `NotReady` 지속 | CNI(calico) 미기동 | 위 calico 상태 확인 |

---

## 4. 노드 내부 CA 동작 확인

### 왜 필요한가

3단계에서 calico가 떴다면 사실상 확인된 것이지만, **가로채는 레지스트리(registry.k8s.io)에서 노드가 직접 이미지를 받는지** 한 번 더 명시적으로 확인한다. 이후 이미지 pull 실패가 나면 CA 문제를 바로 배제할 수 있다.

### 실행·확인

```bash
minikube image pull registry.k8s.io/pause:3.10
```

- 오류 없이 끝나면 성공

```bash
minikube ssh -- curl -sS -o /dev/null -w '%{http_code}\n' https://registry.k8s.io/v2/
```

- `200` 또는 `401`이면 TLS 통과 (인증 요구는 정상). `curl: (60) SSL certificate problem`이면 CA 미적용

### 실패 시

- 2단계 파일 확인 → `minikube start --embed-certs`로 다시 동기화 → 그래도 안 되면 `minikube delete` 후 3단계부터

---

## 5. metrics-server

### 왜 필요한가

Pod·노드의 CPU·메모리 사용량을 제공하는 `metrics.k8s.io` API를 만든다. 이 프로젝트에서는:

- 메트릭 샘플러가 15초마다 읽어 30분 링버퍼에 저장 (기획서 §8.4)
- 추세 센서(메모리 선형 증가, CPU limit 도달)와 D1·D2 시나리오
- 8단계 StressChaos 스모크 테스트에서 CPU 증가 확인

Prometheus를 쓰지 않기로 했으므로(비목표) 이것이 유일한 메트릭 소스다.

### 실행

```bash
minikube addons enable metrics-server
```

### 확인 (활성화 후 1분 정도 기다림)

```bash
kubectl top nodes
```

- CPU·MEMORY 값이 출력되면 성공. `error: Metrics API not available`이면 아직 기동 중이거나 실패

### 실패 시

- `kubectl get pods -n kube-system -l k8s-app=metrics-server` → `ImagePullBackOff` + `x509`면 4단계로 돌아감

---

## 6. 네임스페이스

### 왜 필요한가

기획서 §6.1의 구성대로 역할을 분리한다.

| 네임스페이스 | 용도 | 분리 이유 |
|---|---|---|
| `dev` | 진단 대상 (템플릿 포크 앱) | 장애 주입 범위를 이 안으로 한정 |
| `kubeguardian` | 진단 에이전트, UI, Phoenix | 진단 대상이 고장 나도 진단 도구는 영향받지 않아야 함. RBAC 주체(ServiceAccount)도 여기 |
| `chaos-mesh` | Chaos Mesh | 평가 전용. 애드온 배포물에 포함하지 않음 |

### 실행

```bash
kubectl create ns dev && kubectl create ns kubeguardian && kubectl create ns chaos-mesh
```

### 확인

```bash
kubectl get ns dev kubeguardian chaos-mesh
```

---

## 7. Chaos Mesh 설치

### 왜 필요한가

시나리오 B1·C2·C3·D1·D2·E1은 **코드 수정 없이 특정 Pod에만** 장애를 일으켜야 한다 (Pod 1개만 HTTP 500, Pod 1개만 시계 어긋남 등). Chaos Mesh는 CRD(HTTPChaos, StressChaos 등)로 장애를 선언하면 각 노드의 `chaos-daemon`이 컨테이너 런타임 소켓을 통해 대상 컨테이너의 네임스페이스에 들어가 장애를 주입한다. 그래서 **런타임 종류와 소켓 경로를 정확히** 알려 줘야 한다.

| 구성 요소 | 역할 |
|---|---|
| chaos-controller-manager | CRD를 감시하고 주입을 지시 |
| chaos-daemon (DaemonSet, 권한 있음) | 노드에서 실제로 주입 |
| chaos-dashboard | 웹 UI (선택) |

### 실행

3단계 확인에서 CONTAINER-RUNTIME이 `docker://`였다면 아래 값을 쓴다. 차트 기본값이 docker이지만 의도를 드러내기 위해 명시한다.

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org && helm repo update
```

```bash
helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --version 2.8.4 --set chaosDaemon.runtime=docker --set chaosDaemon.socketPath=/var/run/docker.sock
```

> 런타임이 `containerd://`로 나왔다면: `--set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/run/containerd/containerd.sock`

### 확인

```bash
kubectl get pods -n chaos-mesh -l app.kubernetes.io/instance=chaos-mesh
```

- controller-manager, chaos-daemon, dashboard 모두 `Running`

```bash
kubectl get crd | grep chaos-mesh.org
```

- httpchaos, stresschaos, timechaos, networkchaos 등이 보이면 성공

### 실패 시

- chaos-daemon `CrashLoopBackOff` → 로그에서 소켓 경로 오류 확인: `kubectl logs -n chaos-mesh -l app.kubernetes.io/component=chaos-daemon`

---

## 8. Chaos Mesh 스모크 테스트 (4종)

### 왜 필요한가

Chaos Mesh는 **arm64를 공식 CI로 테스트하지 않으며**([#3173](https://github.com/chaos-mesh/chaos-mesh/issues/3173)), 메인테이너는 arm64를 공식 지원하지 않겠다고 밝힐 계획이다([#5015](https://github.com/chaos-mesh/chaos-mesh/issues/5015)). **설치가 성공해도 주입이 실패할 수 있다.** 앱을 올리기 전에 더미 Pod로 시나리오에 쓰는 4종을 확인하고, 안 되는 종류는 대체 수단을 정한다.

| 종류 | 쓰는 시나리오 | 알려진 위험 | 실패 시 대체 |
|---|---|---|---|
| HTTPChaos | B1 | arm64 주입 실패 사례 [#3833](https://github.com/chaos-mesh/chaos-mesh/issues/3833) (원인은 네트워크 설정 쪽으로 보임) | 포크에 Pod별 오류 플래그, 또는 toxiproxy 사이드카 |
| StressChaos | D1, D2 | **minikube + arm64에서 cgroup 오류로 실패 사례** [#4406](https://github.com/chaos-mesh/chaos-mesh/issues/4406) | stress-ng 사이드카, D1은 k6 부하 + 낮은 CPU limit |
| TimeChaos | C3 | 2022년 arm64 지원 추가 [#2634](https://github.com/chaos-mesh/chaos-mesh/issues/2634) | (위험 낮음) |
| NetworkChaos | C2, E1 | 노드 커널 `sch_netem` 필요 [#3604](https://github.com/chaos-mesh/chaos-mesh/issues/3604) | NetworkPolicy (3단계 calico로 준비됨) |

### 준비: 테스트 대상 Pod

테스트 이미지는 모두 docker.io에서 받는다 (프록시가 가로채지 않음).

```bash
kubectl create ns chaos-smoke
```

```bash
kubectl apply -n chaos-smoke -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
        - name: nginx
          image: nginx:1.27
          ports: [{containerPort: 80}]
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 80}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: clock}
spec:
  replicas: 1
  selector: {matchLabels: {app: clock}}
  template:
    metadata: {labels: {app: clock}}
    spec:
      containers:
        - name: clock
          image: busybox:1.36
          command: ["sh", "-c", "while true; do date; sleep 5; done"]
---
apiVersion: v1
kind: Pod
metadata: {name: client}
spec:
  containers:
    - name: curl
      image: curlimages/curl:8.10.1
      command: ["sleep", "infinity"]
EOF
```

```bash
kubectl wait -n chaos-smoke --for=condition=Ready pod --all --timeout=120s
```

**기준선** — 장애 없이 응답 코드와 시간을 기록해 둔다.

```bash
kubectl exec -n chaos-smoke client -- curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' http://web
```

- `200 0.00xs` 수준

### 8-1. HTTPChaos — 응답 코드를 500으로 바꿈

```bash
kubectl apply -n chaos-smoke -f - <<'EOF'
apiVersion: chaos-mesh.org/v1alpha1
kind: HTTPChaos
metadata: {name: smoke-http}
spec:
  mode: all
  selector: {labelSelectors: {app: web}}
  target: Response
  port: 80
  path: "*"
  replace: {code: 500}
  duration: 2m
EOF
```

**확인** (10초 뒤)

```bash
kubectl exec -n chaos-smoke client -- curl -s -o /dev/null -w '%{http_code}\n' http://web
```

- `500`이면 성공. `200`이면 실패 → `kubectl describe httpchaos -n chaos-smoke smoke-http`의 Events 확인

### 8-2. StressChaos — CPU 부하

```bash
kubectl apply -n chaos-smoke -f - <<'EOF'
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata: {name: smoke-stress}
spec:
  mode: one
  selector: {labelSelectors: {app: web}}
  stressors:
    cpu: {workers: 1, load: 80}
  duration: 2m
EOF
```

**확인** (30~60초 뒤, metrics-server 갱신 주기 때문)

```bash
kubectl top pod -n chaos-smoke -l app=web
```

- CPU가 기준선(수 m)보다 크게 오르면(수백 m) 성공. Events에 `controller is not supported`면 #4406 문제

### 8-3. TimeChaos — 시계 10분 뒤로

```bash
kubectl apply -n chaos-smoke -f - <<'EOF'
apiVersion: chaos-mesh.org/v1alpha1
kind: TimeChaos
metadata: {name: smoke-time}
spec:
  mode: one
  selector: {labelSelectors: {app: clock}}
  timeOffset: "-10m"
  duration: 2m
EOF
```

**확인** (15초 뒤)

```bash
kubectl logs -n chaos-smoke deploy/clock --tail=6
```

- 출력되는 시각이 중간에 약 10분 뒤로 점프하면 성공 (TimeChaos는 컨테이너의 PID 1과 그 자식 프로세스에만 적용됨)

### 8-4. NetworkChaos — 500ms 지연

```bash
kubectl apply -n chaos-smoke -f - <<'EOF'
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata: {name: smoke-net}
spec:
  action: delay
  mode: all
  selector: {labelSelectors: {app: web}}
  delay: {latency: 500ms}
  duration: 2m
EOF
```

**확인** (10초 뒤)

```bash
kubectl exec -n chaos-smoke client -- curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' http://web
```

- 응답 시간이 0.5초 이상이면 성공. Events에 `Specified qdisc not found`면 커널에 netem이 없는 것

### 결과 기록

| 종류 | 결과 | 비고 (오류 메시지·대체 결정) |
|---|---|---|
| HTTPChaos | ✅ 성공 | 200 → 500 (3회 모두), 해제 후 200 (10-13) |
| StressChaos | ✅ 성공 | web Pod CPU 약 1m → 517m, 노드에서 stress-ng-cpu 약 80% 확인 |
| TimeChaos | ❌ 실패 | busybox 예시: 주입은 됐지만(AllInjected) 로그 시각이 바뀌지 않음. `date`가 루프마다 새로 실행되는 프로세스라 주입 대상이 아님. 단일 프로세스(python)로 재시험하자 주입 직후 대상 컨테이너가 종료 코드 139(세그멘테이션 오류)로 재시작됨 → arm64에서 시계 주입이 프로세스를 깨뜨리는 것으로 판단. 사용처는 C3(확장 후보)뿐이라 MVP 영향 없음. C3를 진행하면 포크 앱에 libfaketime 등 대체 수단 검토 |
| NetworkChaos | ✅ 성공 | delay 500ms: 응답 0.006s → 1.0s. C2용 partition도 추가 확인: 차단 중 시간 초과(5s), 해제 후 200 |

판단 기준:
- 4종 성공 → 그대로 진행
- 1~2종 실패 → 위 표의 대체 수단으로 해당 시나리오 주입 방식 변경, ADR로 기록
- 3종 이상 실패 → `--driver=vfkit`로 클러스터를 다시 만들어 재시도. 그래도 안 되면 amd64 원격 VM 검토

### 정리

```bash
kubectl delete ns chaos-smoke
```

---

## 9. Pod 안에서 진단용 Azure OpenAI 호출 (2026-10-19 확인)

진단 에이전트가 클러스터 안에서 LLM을 부를 수 있는지 확인한다 (이슈 #8). 진단 대상 llm-gateway와는 다른 경로다.

### 연결 정보와 모델

- 엔드포인트·키·API 버전은 `.env`의 `AZURE_OPENAI_*`. 클러스터에서는 `kubeguardian` 네임스페이스의 Secret `kg-azure-openai`로 넣는다.
- 모델(배포) 이름은 `.env`에 두지 않는다. 에이전트 버전의 `config.py`가 역할별로 고르고, `core/llm.py`의 `build_chat_model(model)`이 만든다.
- 모델마다 다른 API 제약은 `core/llm.py`의 `MODEL_OPTIONS`에 모은다. `gpt-6.1-sol`은 temperature 0을 거부하고(기본값 1만 허용), 도구 호출이 Responses API에서만 되므로 `AZURE_OPENAI_API_VERSION`이 `2025-03-01-preview` 이상이어야 한다 (`2025-04-01-preview`로 확인).

### Pod용 CA 주입

- 사내 CA(`~/.minikube/certs/sk-ca.pem`)를 ConfigMap `kg-ca-bundle`로 마운트한다.
- 컨테이너 시작 시 공개 CA(certifi)와 사내 CA를 합친 번들을 만들고 `SSL_CERT_FILE`로 지정한다. `SSL_CERT_FILE`은 기본 저장소를 대체하므로 둘을 함께 넣어야 한다.
- Mac에서는 사내 CA 없이도 호출이 성공했다. Pod에서는 번들을 넣은 상태로만 확인했다.

### 실행

```bash
scripts/llm-spike/run.sh gpt-6.1-sol
```

- 성공: `OK <초>s reply='ping'` (Pod 안 약 2~3초)
- 키 값은 출력하지 않는다. Secret은 `.env`에서 `LLM_PROVIDER`·`AZURE_OPENAI_*`만 골라 만든다.

## 10. 진단 대상 기동과 정상 기준선 (2026-10-19 확인)

템플릿 포크를 `dev`에 띄우고 로그인 → 채팅이 실제 Azure OpenAI 응답으로 성공하는지 확인한다 (이슈 #7, 시나리오 H0).

### 순서

| 단계 | 명령 | 하는 일 |
|---|---|---|
| ① 기동 | `make up` | 로컬 비밀값·CA 번들 생성(없을 때만), 이미지 빌드, overlay 적용, 전 Deployment 준비까지 대기 |
| ② LLM 배포 등록 | 아래 curl (직접 실행) | llm-gateway DB에 템플릿용 Azure OpenAI 배포를 넣는다. 키는 프롬프트로만 입력한다 |
| ③ 시드 | `make seed` | 조직 `kg-test`, 테스트 사용자 `kg-tester`, 조직에 모델 할당. 여러 번 실행해도 같다 |
| ④ 스모크 | `make smoke` | front-chat 경유 로그인 → `/api/v1/agent/invoke`. 성공하면 `OK <초>s model=gpt-4.1 answer=...` |

- `make down`은 postgres 데이터까지 지운다. 다시 띄우면 ②부터 다시 한다.
- 테스트 사용자 계정은 `.env.test-user`(git 제외)에 만들어진다. 비밀번호는 출력하지 않는다.
- 템플릿 agent는 LLM 호출이 실패해도 HTTP 200에 `오류가 발생했습니다: ...`를 answer로 돌려준다. 스모크는 이 문구를 실패로 본다.

### ② LLM 배포 등록

템플릿 llm-gateway는 chat completions만 쓰고 API 버전이 `2024-10-21`로 고정되어 있어 gpt-4.1 계열을 등록한다. 진단 에이전트용 키(`.env`)와는 별개다.

```bash
kubectl -n dev port-forward svc/gateway 18000:8000
```

다른 터미널에서 superadmin으로 로그인한다. 초기 비밀번호는 `backend-admin/app/main.py`에 있다.

```bash
PW=$(sed -nE 's/.*password="([^"]*)".*S106.*/\1/p' ~/Cursor/kg-target/backend-admin/app/main.py); TOKEN=$(curl -s localhost:18000/api/v1/auth/login -H 'Content-Type: application/json' -d "{\"id\":\"superadmin\",\"password\":\"$PW\"}" | jq -r .access_token); [ -n "$TOKEN" ] && [ "$TOKEN" != null ] && echo "로그인 OK" || echo "로그인 실패"
```

```bash
read -s "KEY?템플릿용 Azure 키: "; echo; curl -s localhost:18000/api/v1/llm-gateway/deployments -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"provider\":\"azure-openai\",\"model_name\":\"gpt-4.1\",\"model_version\":\"4.1\",\"deployment_name\":\"gpt-4.1\",\"endpoint\":\"<엔드포인트>\",\"access_key\":\"$KEY\",\"is_active\":true}" | jq 'del(.. | .access_key?)'; unset KEY
```

- 키는 명령에 직접 적지 않는다. 적으면 셸 history에 남는다.
- `read -s "KEY?..."`는 zsh 문법이다. bash에서는 `read -rsp '템플릿용 Azure 키: ' KEY`를 쓴다.

### Phoenix (실행 추적)

- `make phoenix`: `kubeguardian` 네임스페이스에 Phoenix(`arizephoenix/phoenix:version-20.23.0`)를 띄운다. 데이터는 emptyDir라 Pod가 다시 뜨면 비워진다.
- 화면: `kubectl -n kubeguardian port-forward svc/phoenix 6006:6006` → http://localhost:6006
- 클러스터 안 수집 주소: `http://phoenix.kubeguardian.svc:6006/v1/traces`(HTTP), `phoenix.kubeguardian.svc:4317`(gRPC). 로컬 실행 시 `.env`의 `OTEL_EXPORTER_OTLP_ENDPOINT`는 port-forward한 `http://localhost:6006/v1/traces`
- Service 이름이 `phoenix`라 K8s가 `PHOENIX_PORT=tcp://...`를 자동 주입한다. 매니페스트에서 `PHOENIX_PORT=6006`으로 덮어쓴다 (없으면 기동 실패)

### 실패 시

- `토큰 검증에 실패했습니다` → 로그인이 실패해 `TOKEN`이 `null`이다. 로그인 명령의 출력을 확인한다
- 스모크 `LLM Gateway error: 500` + llm-gateway 로그 `CERTIFICATE_VERIFY_FAILED` → 사내 CA 번들이 없다. `make ca` 후 `make up`
- 스모크 `LLM Gateway error: 403` → 조직에 그 모델이 할당되지 않았다. 배포를 등록한 뒤 `make seed`
- 스모크 `로그인 실패: kg-tester` → `.env.test-user`를 새로 만들었는데 사용자는 DB에 이전 비밀번호로 남아 있다. `make down` → `make up`부터 다시 하거나, 파일을 지우지 않는다

## 운영 메모

| 상황 | 명령 |
|---|---|
| 작업 종료 (상태 보존) | `minikube stop` |
| 다시 시작 | `minikube start` (처음 옵션이 저장되어 있음) |
| 완전 초기화 | `minikube delete` 후 3단계부터 |
| Docker Desktop 재시작 후 | `minikube start`로 노드 컨테이너 재기동 |
| Chaos Mesh 대시보드 | `kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333` → http://localhost:2333 |

Mac의 docker 드라이버에서는 NodePort로 바로 접근할 수 없다. 서비스 접근은 `kubectl port-forward` 또는 `minikube service <이름> -n <ns>`를 쓴다.

## 다음 단계 (별도 문서)

1. ~~**Pod 안에서 Azure OpenAI 호출**~~ — 9단계로 완료
2. ~~**템플릿 포크 배포**~~ — 10단계로 완료
