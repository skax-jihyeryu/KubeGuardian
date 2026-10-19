#!/usr/bin/env bash
# reset.sh: A1 — inject.sh가 저장한 원래 selector로 되돌린다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
SVC="${A1_SERVICE:-agent}"
ANNOTATION="kubeguardian.io/a1-original-selector"

original="$(kubectl -n "$NS" get service "$SVC" -o jsonpath="{.metadata.annotations.kubeguardian\.io/a1-original-selector}")"
if [[ -z "$original" ]]; then
  echo "주입 기록(annotation $ANNOTATION)이 없다. 되돌릴 것이 없다."
  exit 0
fi

# merge patch는 키를 더하기만 하므로 selector 전체를 json patch로 바꾼다
kubectl -n "$NS" patch service "$SVC" --type json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/selector\",\"value\":$original}]"
kubectl -n "$NS" annotate service "$SVC" "$ANNOTATION-"
echo "A1 원복: $NS/Service/$SVC selector = $original"
