#!/usr/bin/env bash
# reset.sh: A5 — inject.sh가 저장한 원래 서비스 주소 env로 되돌린다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY=gateway
STATE="$(cd "$(dirname "$0")" && pwd)/.state/$NS-$DEPLOY.env"

if [[ ! -f "$STATE" ]]; then
  echo "주입 기록($STATE)이 없다. 되돌릴 것이 없다."
  exit 0
fi
original=()
while IFS= read -r line; do original+=("$line"); done < "$STATE"

kubectl -n "$NS" set env "deployment/$DEPLOY" "${original[@]}" >/dev/null
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=180s >/dev/null
rm -- "$STATE"
echo "A5 원복: $NS/Deployment/$DEPLOY 서비스 주소 env ${#original[@]}개"
