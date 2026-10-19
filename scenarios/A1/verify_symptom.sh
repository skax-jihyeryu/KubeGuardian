#!/usr/bin/env bash
# verify_symptom.sh: A1 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. agent Service의 EndpointSlice에 엔드포인트가 하나도 없다 (selector에 맞는 Pod 없음)
# 2. agent Pod 자체는 Ready로 남아 있다 (원인이 Pod가 아니라 Service 쪽임을 보장)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
SVC="${A1_SERVICE:-agent}"
POD_SELECTOR="${A1_POD_SELECTOR:-app=agent}"

addresses="$(kubectl -n "$NS" get endpointslices -l "kubernetes.io/service-name=$SVC" \
  -o jsonpath='{.items[*].endpoints[*].addresses[*]}')"
if [[ -n "$addresses" ]]; then
  echo "증상 없음: $NS/Service/$SVC 엔드포인트가 남아 있다 ($addresses)" >&2
  exit 1
fi

ready="$(kubectl -n "$NS" get pods -l "$POD_SELECTOR" \
  -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c '^True$' || true)"
if [[ "$ready" -eq 0 ]]; then
  echo "무효: $POD_SELECTOR Ready Pod가 없다 (다른 원인이 섞임)" >&2
  exit 1
fi

echo "증상 확인: $NS/Service/$SVC 엔드포인트 0개, $POD_SELECTOR Ready Pod ${ready}개"
