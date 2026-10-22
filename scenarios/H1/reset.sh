#!/usr/bin/env bash
# reset.sh: H1 — h1-noconfig Deployment·Service를 지우고 사라질 때까지 기다린다 (최대 60초)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"

kubectl -n "$NS" delete deployment/h1-noconfig service/h1-noconfig --ignore-not-found --wait=false >/dev/null
for _ in $(seq 1 30); do
  left="$(kubectl -n "$NS" get deploy,svc,pods -o name | grep -c 'h1-' || true)"
  if [[ "$left" -eq 0 ]]; then
    echo "H1 원복: h1- 리소스 없음"
    exit 0
  fi
  sleep 2
done
echo "H1 원복 실패: 60초 뒤에도 h1- 리소스 ${left}개가 남아 있다" >&2
exit 1
