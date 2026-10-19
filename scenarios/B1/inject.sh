#!/usr/bin/env bash
# inject.sh: B1 — agent Pod 1개만 HTTP 500 (Chaos Mesh HTTPChaos, mode: one)
#
# agent Pod 중 하나를 골라 그 Pod의 HTTP 응답 코드를 전부 500으로 바꾼다. Pod는 Ready로 남는다.
# Chaos 객체는 진단 대상 밖(chaos-mesh 네임스페이스)에 만든다. 대상 네임스페이스에 정답이 보이지 않게 하기 위해서다.
# Chaos Mesh는 대상 Pod 네임스페이스에 PodHttpChaos를 만든다. 주입 중에는 남고, reset.sh가 지운다
#
# 환경 변수
#   KG_NAMESPACE      대상 네임스페이스 (기본 dev)
#   KG_CHAOS_NS       Chaos 객체 네임스페이스 (기본 chaos-mesh)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME=kg-http-1  # 시나리오 ID를 넣지 않는다 (이름에 정답이 드러나지 않게)

if kubectl -n "$CHAOS_NS" get httpchaos "$NAME" >/dev/null 2>&1; then
  echo "이미 주입된 상태다 ($CHAOS_NS/HTTPChaos/$NAME 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

kubectl apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: HTTPChaos
metadata:
  name: $NAME
  namespace: $CHAOS_NS
spec:
  mode: one
  selector:
    namespaces: [$NS]
    labelSelectors: {app: agent}
  target: Response
  port: 8006
  path: "*"
  replace: {code: 500}
EOF

for _ in $(seq 30); do
  injected="$(kubectl -n "$CHAOS_NS" get httpchaos "$NAME" \
    -o jsonpath='{.status.conditions[?(@.type=="AllInjected")].status}')"
  [[ "$injected" == True ]] && break
  sleep 2
done
if [[ "$injected" != True ]]; then
  echo "주입 실패: $CHAOS_NS/HTTPChaos/$NAME AllInjected=$injected" >&2
  exit 1
fi
pod="$(kubectl -n "$CHAOS_NS" get httpchaos "$NAME" -o jsonpath='{.status.experiment.containerRecords[0].id}')"
echo "B1 주입: $pod 응답 코드 → 500 ($CHAOS_NS/HTTPChaos/$NAME)"
