#!/usr/bin/env bash
# reset.sh: C4 — gateway PROXY_TIMEOUT을 원래대로 되돌린다
#
# inject.sh가 .state/original_proxy_timeout에 남긴 원래 값으로 복원한다. 비어 있으면(원래 env 없음) env를 제거한다.
# 기록이 없어도 env를 제거한다 (이전 실행이 중간에 끝났을 수 있다. 이 환경에서 원래 PROXY_TIMEOUT env는 없다)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY=gateway
FILE="$(dirname "$0")/.state/original_proxy_timeout"

original=""
[[ -f "$FILE" ]] && original="$(cat "$FILE")"

current="$(kubectl -n "$NS" get "deploy/$DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="gateway")].env[?(@.name=="PROXY_TIMEOUT")].value}')"
if [[ -n "$original" ]]; then
  kubectl -n "$NS" set env "deploy/$DEPLOY" "PROXY_TIMEOUT=$original" >/dev/null
  echo "C4 원복: PROXY_TIMEOUT=${original}"
elif [[ -n "$current" ]]; then
  kubectl -n "$NS" set env "deploy/$DEPLOY" PROXY_TIMEOUT- >/dev/null
  echo "C4 원복: PROXY_TIMEOUT 제거 (현재 ${current})"
else
  echo "주입 기록이 없고 PROXY_TIMEOUT env도 없다."
fi
rm -f "$FILE"
kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=180s >/dev/null
for _ in $(seq 60); do
  terminating="$(kubectl -n "$NS" get pods -l "app=$DEPLOY" \
    -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' || true)"
  [[ -z "$terminating" ]] && break
  sleep 2
done
echo "${NS}/Deployment/${DEPLOY} 롤아웃 완료"
