#!/usr/bin/env bash
# reset.sh: B1 — HTTPChaos를 지워 agent Pod 응답을 되돌린다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME="kg-b1-$NS"

if ! kubectl -n "$CHAOS_NS" get httpchaos "$NAME" >/dev/null 2>&1; then
  echo "주입 기록($CHAOS_NS/HTTPChaos/$NAME)이 없다. 되돌릴 것이 없다."
  exit 0
fi
# Chaos Mesh가 Pod의 주입을 걷어낸 뒤에 객체가 사라진다 (finalizer)
kubectl -n "$CHAOS_NS" delete httpchaos "$NAME" --wait=true --timeout=120s >/dev/null
echo "B1 원복: $CHAOS_NS/HTTPChaos/$NAME 삭제"
