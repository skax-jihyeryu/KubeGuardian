#!/usr/bin/env bash
# inject.sh: A1 — agent Service selector 오타
#
# agent Service의 selector 중 한 키(기본 app)의 값 끝에 "x"를 붙인다 (예: agent → agentx).
# 원래 selector는 Service annotation에 저장해 reset.sh가 되돌린다.
#
# 환경 변수
#   KG_NAMESPACE   대상 네임스페이스 (기본 dev)
#   A1_SERVICE     대상 Service 이름 (기본 agent)
#   A1_KEY         바꿀 selector 키 (기본 app)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
SVC="${A1_SERVICE:-agent}"
KEY="${A1_KEY:-app}"
ANNOTATION="kubeguardian.io/a1-original-selector"

if [[ -n "$(kubectl -n "$NS" get service "$SVC" -o jsonpath="{.metadata.annotations.kubeguardian\.io/a1-original-selector}")" ]]; then
  echo "이미 주입된 상태다 (annotation $ANNOTATION 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

original="$(kubectl -n "$NS" get service "$SVC" -o jsonpath='{.spec.selector}')"
value="$(kubectl -n "$NS" get service "$SVC" -o jsonpath="{.spec.selector.$KEY}")"
if [[ -z "$value" ]]; then
  echo "Service $NS/$SVC selector에 키 $KEY가 없다: $original" >&2
  exit 1
fi

kubectl -n "$NS" annotate service "$SVC" "$ANNOTATION=$original"
kubectl -n "$NS" patch service "$SVC" --type merge \
  -p "{\"spec\":{\"selector\":{\"$KEY\":\"${value}x\"}}}"
echo "A1 주입: $NS/Service/$SVC selector.$KEY = ${value}x (원래 $value)"
