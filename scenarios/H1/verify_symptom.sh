#!/usr/bin/env bash
# verify_symptom.sh: H1 — "증상"은 정상 동작이다. h1-noconfig Pod가 Ready이고 Service 엔드포인트가 1개이며
# H0 스모크(로그인 → 채팅)가 성공해야 한다.
set -euo pipefail
cd "$(dirname "$0")/../.."

NS="${KG_NAMESPACE:-dev}"
ready="$(kubectl -n "$NS" get pods -l app=h1-noconfig \
  -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c '^True$' || true)"
if [[ "$ready" -ne 1 ]]; then
  echo "h1-noconfig Ready Pod가 ${ready}개다 (1개여야 한다)" >&2
  exit 1
fi
endpoints="$(kubectl -n "$NS" get endpointslices -l kubernetes.io/service-name=h1-noconfig \
  -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' | grep -c '^true$' || true)"
if [[ "$endpoints" -ne 1 ]]; then
  echo "h1-noconfig 엔드포인트가 ${endpoints}개다 (1개여야 한다)" >&2
  exit 1
fi
echo "H1 정상: Ready Pod ${ready}개, 엔드포인트 ${endpoints}개"
exec bash scripts/target/smoke.sh
