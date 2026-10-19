#!/usr/bin/env bash
# inject.sh: C2 — admin → Redis 연결 차단 (Chaos Mesh NetworkChaos partition)
#
# admin Pod와 redis Pod 사이 패킷을 양방향으로 버린다. 두 Pod 모두 Ready로 남는다.
# Chaos 객체는 진단 대상 밖(chaos-mesh 네임스페이스)에 만든다. 대상 네임스페이스에 정답이 보이지 않게 하기 위해서다.
# (Chaos Mesh가 대상 Pod 네임스페이스에 PodNetworkChaos를 만드는 것은 피할 수 없다. KubeGuardian은 이 kind를 수집하지 않는다)
#
# 환경 변수
#   KG_NAMESPACE      대상 네임스페이스 (기본 dev)
#   KG_CHAOS_NS       Chaos 객체 네임스페이스 (기본 chaos-mesh)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME="kg-c2-$NS"

if kubectl -n "$CHAOS_NS" get networkchaos "$NAME" >/dev/null 2>&1; then
  echo "이미 주입된 상태다 ($CHAOS_NS/NetworkChaos/$NAME 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

# direction: both — 요청은 Service ClusterIP로 나가므로 admin 쪽 규칙만으로는(목적지가 Pod IP가 아님) 맞지 않을 수 있다.
# redis 쪽에서도 admin Pod IP를 막아 확실히 끊는다
kubectl apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: $NAME
  namespace: $CHAOS_NS
spec:
  action: partition
  mode: all
  selector:
    namespaces: [$NS]
    labelSelectors: {app: admin}
  direction: both
  target:
    mode: all
    selector:
      namespaces: [$NS]
      labelSelectors: {app: redis}
EOF

for _ in $(seq 30); do
  injected="$(kubectl -n "$CHAOS_NS" get networkchaos "$NAME" \
    -o jsonpath='{.status.conditions[?(@.type=="AllInjected")].status}')"
  [[ "$injected" == True ]] && break
  sleep 2
done
if [[ "$injected" != True ]]; then
  echo "주입 실패: $CHAOS_NS/NetworkChaos/$NAME AllInjected=$injected" >&2
  exit 1
fi
echo "C2 주입: $NS admin ↔ redis 네트워크 차단 ($CHAOS_NS/NetworkChaos/$NAME)"
