.PHONY: setup lint format test check run check-run doctor secret ca build up down seed smoke phoenix eval kg-build kg-up kg-down

# 진단 대상(템플릿 포크) 위치. 포크 6개 + shared-infra가 이 아래에 있어야 한다.
TARGET_DIR ?= $(HOME)/Cursor/kg-target
TARGET_OVERLAY := $(TARGET_DIR)/shared-infra/kubernetes/local
TARGET_SECRET := $(TARGET_OVERLAY)/secret.env
TARGET_CA := $(TARGET_OVERLAY)/ca-bundle.pem
# 사내 SSL 프록시 CA (환경 설치 가이드 2단계)
CORP_CA ?= $(HOME)/.minikube/certs/sk-ca.pem
TARGET_NS := dev
TARGET_BACKENDS := gateway admin agent llm-gateway
TARGET_FRONTENDS := chat admin
comma := ,
space := $(subst ,, )
TARGET_DEPLOYS := postgres redis $(TARGET_BACKENDS) $(addprefix front-,$(TARGET_FRONTENDS))
# minikube docker-env로 빌드하면 베이스 이미지 pull이 시간 초과로 실패해, 노드에서 먼저 받아 둔다.
TARGET_BASE_IMAGES := python:3.12-slim ghcr.io/astral-sh/uv:0.9.13 node:20 nginx:1.24.0-alpine postgres:16-alpine redis:7-alpine

setup:  ## 의존성 설치 + git 훅 설치
	uv sync
	uv run pre-commit install

lint:  ## lint·format 검사
	uv run ruff check .
	uv run ruff format --check .

format:  ## 자동 수정
	uv run ruff check --fix .
	uv run ruff format .

test:  ## 단위 테스트
	uv run pytest

check: lint test  ## PR 올리기 전 확인

run:  ## API 서버 로컬 실행
	cd app && uv run --env-file ../.env python main.py

check-run:  ## 규칙 점검 한 번 실행 (kg check). 예: make check-run ARGS="--config kubeguardian.yaml"
	uv run python app/cli.py check $(ARGS)

doctor:  ## 로컬 도구·자원 확인 (minikube, kubectl, docker, 메모리)
	@for t in uv minikube kubectl docker; do \
		command -v $$t >/dev/null && echo "ok      $$t" || echo "missing $$t"; \
	done
	@echo "memory  $$(( $$(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))GB (12GB 이상 필요)"

secret:  ## 진단 대상 로컬 비밀값 생성 (없을 때만, 값은 출력하지 않음)
	@test -f $(TARGET_SECRET) || { \
		umask 077; \
		for k in MASTER_KEY DB_PASSWORD REDIS_ACCESS_KEY; do v=$$(openssl rand -hex 24) || exit 1; echo "$$k=$$v"; done > $(TARGET_SECRET).tmp \
			&& mv $(TARGET_SECRET).tmp $(TARGET_SECRET) || { rm -f $(TARGET_SECRET).tmp; exit 1; }; \
		echo "created $(TARGET_SECRET)"; \
	}

ca:  ## 진단 대상 llm-gateway용 CA 번들 생성 (공개 CA + 사내 CA, 없을 때만)
	@test -f $(TARGET_CA) || { \
		certifi=$$(uv run python -c 'import certifi; print(certifi.where())') || exit 1; \
		cat "$$certifi" $(CORP_CA) > $(TARGET_CA).tmp && mv $(TARGET_CA).tmp $(TARGET_CA) || { rm -f $(TARGET_CA).tmp; exit 1; }; \
		echo "created $(TARGET_CA)"; \
	}

build:  ## 진단 대상 이미지를 minikube 노드의 docker에 빌드
	@for img in $(TARGET_BASE_IMAGES); do minikube ssh -- docker pull -q $$img >/dev/null || exit 1; done
	@eval $$(minikube docker-env) && \
	for s in $(TARGET_BACKENDS); do \
		echo "build kg-target/$$s:local"; \
		docker build -q -t kg-target/$$s:local $(TARGET_DIR)/backend-$$s >/dev/null || exit 1; \
	done && \
	for s in $(TARGET_FRONTENDS); do \
		echo "build kg-target/front-$$s:local"; \
		docker build -q -t kg-target/front-$$s:local -f $(TARGET_DIR)/frontend/$$s/Dockerfile $(TARGET_DIR)/frontend >/dev/null || exit 1; \
	done

up: secret ca build  ## 진단 대상 환경 기동 (dev 네임스페이스, 전 Pod Ready까지 대기)
	@# CA 번들 ConfigMap이 커서 client-side apply의 annotation 한도(256KiB)에 가깝다. server-side apply로 적용한다.
	@# 이미지 태그(:local)가 같아 다시 빌드해도 Pod가 바뀌지 않으므로, 이미 떠 있던 앱 Deployment는 재시작한다.
	@existed=$$(kubectl -n $(TARGET_NS) get deployment gateway -o name 2>/dev/null); \
	kubectl apply --server-side --force-conflicts -k $(TARGET_OVERLAY) || exit 1; \
	if [ -n "$$existed" ]; then \
		kubectl -n $(TARGET_NS) rollout restart $(addprefix deployment/,$(TARGET_BACKENDS) $(addprefix front-,$(TARGET_FRONTENDS))); \
	fi
	@for d in $(TARGET_DEPLOYS); do kubectl -n $(TARGET_NS) rollout status deployment/$$d --timeout=300s || exit 1; done
	kubectl -n $(TARGET_NS) get pods

down: secret ca  ## 진단 대상 환경 삭제 (dev 네임스페이스의 overlay 리소스, DB 데이터 포함)
	kubectl delete -k $(TARGET_OVERLAY) --ignore-not-found
	@if kubectl -n $(TARGET_NS) get pods -l 'app in ($(subst $(space),$(comma),$(TARGET_DEPLOYS)))' --no-headers 2>/dev/null | grep -q .; then \
		kubectl -n $(TARGET_NS) wait --for=delete pod -l 'app in ($(subst $(space),$(comma),$(TARGET_DEPLOYS)))' --timeout=120s; \
	fi
	kubectl -n $(TARGET_NS) get all

seed:  ## 진단 대상 테스트 조직·사용자·LLM 할당 (LLM 배포는 먼저 등록, 환경 설치 가이드 10단계)
	TARGET_DIR=$(TARGET_DIR) TARGET_NS=$(TARGET_NS) bash scripts/target/seed.sh

smoke:  ## 진단 대상 E2E: front-chat 경유 로그인 → 채팅
	TARGET_DIR=$(TARGET_DIR) TARGET_NS=$(TARGET_NS) bash scripts/target/smoke.sh

phoenix:  ## 실행 추적 Phoenix 설치 (kubeguardian 네임스페이스)
	kubectl apply -f deploy/phoenix/phoenix.yaml
	kubectl -n kubeguardian rollout status deployment/phoenix --timeout=300s

# 평가에 쓸 설정. kubeguardian.yaml이 없으면 예시 설정을 쓴다.
EVAL_CONFIG ?= $(if $(wildcard kubeguardian.yaml),kubeguardian.yaml,kubeguardian.example.yaml)

eval:  ## 시나리오 하나 평가 (주입 → 증상 확인 → 점검·게이트 → 채점 → 원복). 예: make eval S=A5, AI 조건은 make eval S=A5 ARGS="--condition ai"
	@test -n "$(S)" || { echo "S=<시나리오 ID>가 필요하다. 예: make eval S=A5"; exit 2; }
	uv run python eval/run.py $(S) --config $(EVAL_CONFIG) $(ARGS)

kg-build:  ## KubeGuardian runner 이미지를 minikube 노드의 docker에 빌드
	@for img in python:3.12-slim ghcr.io/astral-sh/uv:0.9.13; do minikube ssh -- docker pull -q $$img >/dev/null || exit 1; done
	@eval $$(minikube docker-env) && docker build -q -t kubeguardian:local .

# runner에 넣을 점검 설정. kubeguardian.yaml이 없으면 예시 설정을 쓴다.
KG_NS := kubeguardian
KG_ENV_KEYS := LLM_PROVIDER|AZURE_OPENAI_ENDPOINT|AZURE_OPENAI_API_KEY|AZURE_OPENAI_API_VERSION
KG_ENV_COUNT := 4

kg-up: kg-build ca  ## runner Pod 기동 (kubeguardian 네임스페이스, 이미지·Secret·ConfigMap·CA 포함)
	@kubectl get ns $(KG_NS) >/dev/null 2>&1 || kubectl create ns $(KG_NS)
	kubectl apply -f deploy/kubeguardian/rbac.yaml
	@# 비밀값은 .env에서 필요한 키만 골라 Secret으로 만든다. 값은 출력하지 않는다.
	@# server-side apply라 값이 last-applied-configuration annotation에 남지 않는다.
	@set -o pipefail; tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	grep -E '^($(KG_ENV_KEYS))=' .env > "$$tmp"; n=$$(wc -l < "$$tmp" | tr -d ' '); \
	if [ "$$n" != "$(KG_ENV_COUNT)" ]; then echo "키 $$n/$(KG_ENV_COUNT): .env에 필요한 키가 모자라거나 겹친다 (값은 출력하지 않음)"; exit 1; fi; \
	kubectl -n $(KG_NS) create secret generic kubeguardian-env --from-env-file="$$tmp" --dry-run=client -o yaml \
	  | kubectl apply --server-side --force-conflicts -f - >/dev/null && echo "secret/kubeguardian-env applied"
	@# 예전 client-side apply가 남긴 annotation(값 포함)을 지운다. 없으면 무시한다.
	@kubectl -n $(KG_NS) annotate secret kubeguardian-env kubectl.kubernetes.io/last-applied-configuration- >/dev/null 2>&1 || true
	@kubectl -n $(KG_NS) create configmap kubeguardian-config --from-file=kubeguardian.yaml=$(EVAL_CONFIG) --dry-run=client -o yaml | kubectl apply -f -
	@# CA 번들 ConfigMap은 client-side apply의 annotation 한도에 가까워 server-side apply를 쓴다.
	@kubectl -n $(KG_NS) create configmap kubeguardian-ca --from-file=ca-bundle.pem=$(TARGET_CA) --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f - >/dev/null && echo "configmap/kubeguardian-ca applied"
	@# 이미지 태그(:local)가 같아 다시 빌드해도 Pod가 바뀌지 않으므로, 이미 떠 있던 runner는 재시작한다.
	@existed=$$(kubectl -n $(KG_NS) get deployment kg-runner -o name 2>/dev/null); \
	kubectl apply -f deploy/kubeguardian/runner.yaml || exit 1; \
	if [ -n "$$existed" ]; then kubectl -n $(KG_NS) rollout restart deployment/kg-runner; fi
	kubectl -n $(KG_NS) rollout status deployment/kg-runner --timeout=180s

kg-down:  ## runner Pod·Secret·ConfigMap 삭제 (RBAC·Phoenix는 남김)
	kubectl -n $(KG_NS) delete deployment/kg-runner secret/kubeguardian-env configmap/kubeguardian-config configmap/kubeguardian-ca --ignore-not-found
