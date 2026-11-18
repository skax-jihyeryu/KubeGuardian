#!/usr/bin/env bash
# inject.sh: C4 — gateway PROXY_TIMEOUT을 채팅 응답 시간보다 짧게 줄인다
#
# gateway의 업스트림 프록시 시간 제한(`PROXY_TIMEOUT`, 기본 60초)을 로그인 응답 시간(약 0.9~1.0초)보다 길고
# 채팅 응답 시간(약 1.33~1.86초)보다 짧은 값으로 줄인다. 로그인 같은 짧은 요청은 성공하고 채팅만 gateway에서 시간 초과로 끝난다.
# Deployment env만 바꾼다 (롤아웃). Pod는 모두 Ready로 남고 재시작 횟수는 늘지 않는다 (새 Pod가 뜰 뿐이다)
#
# 환경 변수
#   KG_NAMESPACE      대상 네임스페이스 (기본 dev)
#   C4_PROXY_TIMEOUT  주입할 값 (초, 기본 1.2)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
VALUE="${C4_PROXY_TIMEOUT:-1.2}"
DEPLOY=gateway
STATE="$(dirname "$0")/.state"

if [[ -f "$STATE/original_proxy_timeout" ]]; then
  echo "이미 주입된 상태다 ($STATE/original_proxy_timeout 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

# 이미 PROXY_TIMEOUT이 있으면(.state가 사라진 주입 중 상태 포함) 거절한다. 주입값이 '원래 값'으로 기록되는 것을 막는다
envjson="$(kubectl -n "$NS" get "deploy/$DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="gateway")].env[?(@.name=="PROXY_TIMEOUT")]}')"
if [[ -n "$envjson" ]]; then
  echo "중단: gateway에 PROXY_TIMEOUT env가 이미 있다 (${envjson}). reset.sh로 정리하거나 원래 값을 확인한다." >&2
  exit 1
fi

# 원래 값 기록: 비어 있으면 env가 없었다는 뜻이다 (reset.sh가 env를 제거한다)
original="$(kubectl -n "$NS" get "deploy/$DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="gateway")].env[?(@.name=="PROXY_TIMEOUT")].value}')"
mkdir -p "$STATE"
printf '%s\n' "$original" >"$STATE/original_proxy_timeout"

kubectl -n "$NS" set env "deploy/$DEPLOY" "PROXY_TIMEOUT=$VALUE" >/dev/null
kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=180s >/dev/null
# 이전 Pod가 Terminating으로 남아 있으면 다음 확인·점검에 섞이므로 사라질 때까지 기다린다
for _ in $(seq 60); do
  terminating="$(kubectl -n "$NS" get pods -l "app=$DEPLOY" \
    -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' || true)"
  [[ -z "$terminating" ]] && break
  sleep 2
done
echo "C4 주입: ${NS}/Deployment/${DEPLOY} PROXY_TIMEOUT=${VALUE} (원래 값: ${original:-없음})"
