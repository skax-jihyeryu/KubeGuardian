#!/usr/bin/env bash
# reset.sh: A1 — inject.sh가 저장한 원래 selector로 되돌린다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
SVC="${A1_SERVICE:-agent}"
STATE="$(cd "$(dirname "$0")" && pwd)/.state/$NS-$SVC.selector.json"

if [[ ! -f "$STATE" ]]; then
  echo "주입 기록($STATE)이 없다. 되돌릴 것이 없다."
  exit 0
fi
original="$(cat "$STATE")"

# merge patch는 키를 더하기만 하므로 selector 전체를 json patch로 바꾼다
kubectl -n "$NS" patch service "$SVC" --type json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/selector\",\"value\":$original}]"
rm -- "$STATE"
echo "A1 원복: $NS/Service/$SVC selector = $original"
