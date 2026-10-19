#!/usr/bin/env bash
# reset.sh: C2 — NetworkChaos를 지워 admin ↔ redis 차단을 푼다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME="kg-c2-$NS"

if ! kubectl -n "$CHAOS_NS" get networkchaos "$NAME" >/dev/null 2>&1; then
  echo "주입 기록($CHAOS_NS/NetworkChaos/$NAME)이 없다. 되돌릴 것이 없다."
  exit 0
fi
# Chaos Mesh가 Pod의 차단 규칙을 지운 뒤에 객체가 사라진다 (finalizer)
kubectl -n "$CHAOS_NS" delete networkchaos "$NAME" --wait=true --timeout=120s >/dev/null
echo "C2 원복: $CHAOS_NS/NetworkChaos/$NAME 삭제"
