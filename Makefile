.PHONY: setup lint format test check run doctor

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

doctor:  ## 로컬 도구·자원 확인 (minikube, kubectl, docker, 메모리)
	@for t in uv minikube kubectl docker; do \
		command -v $$t >/dev/null && echo "ok      $$t" || echo "missing $$t"; \
	done
	@echo "memory  $$(( $$(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))GB (12GB 이상 필요)"

# 환경·평가 타깃(up, down, inject, reset, eval)은 W1 작업에서 추가한다.
