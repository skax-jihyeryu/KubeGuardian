# KubeGuardian runner 이미지 (minikube 노드 docker에 빌드, imagePullPolicy: Never).
# app/이 Python 루트이고, eval/은 Pod 안 진입점(runner_step.py)이 쓴다.
FROM ghcr.io/astral-sh/uv:0.9.13 AS uv

FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /kg

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ ./app/
COPY eval/*.py ./eval/

ENV PATH="/kg/.venv/bin:$PATH" \
    PYTHONPATH=/kg/app:/kg/eval \
    PYTHONUNBUFFERED=1
