.PHONY: setup lint format test check

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

# 환경·평가 타깃(up, down, inject, reset, eval, blind)은 W1부터 추가한다.
