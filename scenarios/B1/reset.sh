#!/usr/bin/env bash
# reset.sh: B1 — HTTPChaos를 지워 agent Pod 응답을 되돌린다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME=kg-http-1

if kubectl -n "$CHAOS_NS" get httpchaos "$NAME" >/dev/null 2>&1; then
  # Chaos Mesh가 Pod의 주입을 걷어낸 뒤에 객체가 사라진다 (finalizer)
  kubectl -n "$CHAOS_NS" delete httpchaos "$NAME" --wait=true --timeout=120s >/dev/null
  echo "B1 원복: $CHAOS_NS/HTTPChaos/$NAME 삭제"
else
  echo "주입 기록($CHAOS_NS/HTTPChaos/$NAME)이 없다."
fi
# Chaos Mesh가 대상 네임스페이스에 만든 Pod별 객체(spec이 빈 채로 남음)도 지운다. 다음 시나리오에서 흔적이 보이지 않게 한다
kubectl -n "$NS" delete podnetworkchaos,podhttpchaos --all --wait=true >/dev/null
echo "${NS}의 PodNetworkChaos·PodHttpChaos 정리"
