#!/usr/bin/env bash
# reset.sh: A3 — inject.sh가 저장한 원래 이미지·imagePullPolicy로 되돌린다
#
# Pod 템플릿이 원래와 같아지면 Deployment가 이전 ReplicaSet을 다시 쓰므로 Pod는 재시작되지 않는다.
# 롤아웃이 끝나면 주입 이미지를 가진 ReplicaSet(replicas 0)을 지운다. 남겨 두면 다음 시나리오에서
# 조사 에이전트가 이전 주입의 흔적을 읽을 수 있다.
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY="${A3_DEPLOY:-agent}"
CONTAINER="${A3_CONTAINER:-$DEPLOY}"
STATE="$(cd "$(dirname "$0")" && pwd)/.state/$NS-$DEPLOY.image"

if [[ ! -f "$STATE" ]]; then
  echo "주입 기록($STATE)이 없다. 되돌릴 것이 없다."
  exit 0
fi
{ read -r image; read -r policy; } < "$STATE"

kubectl -n "$NS" patch deployment "$DEPLOY" --type strategic -p "$(jq -n \
  --arg c "$CONTAINER" --arg i "$image" --arg p "$policy" \
  '{spec: {template: {spec: {containers: [{name: $c, image: $i, imagePullPolicy: $p}]}}}}')" >/dev/null
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=180s >/dev/null
# 이 Deployment가 소유하고, replicas 0이며, 컨테이너 이미지가 원래 이미지와 다른 ReplicaSet
stale="$(kubectl -n "$NS" get replicasets -o json | jq -r --arg d "$DEPLOY" --arg c "$CONTAINER" --arg i "$image" '
  .items[] | select(any(.metadata.ownerReferences[]?; .kind == "Deployment" and .name == $d))
  | select(.spec.replicas == 0)
  | select(any(.spec.template.spec.containers[]; .name == $c and .image != $i)) | .metadata.name')"
for rs in $stale; do
  kubectl -n "$NS" delete replicaset "$rs" --wait=true >/dev/null
  echo "A3 원복: 주입 ReplicaSet $NS/$rs 삭제"
done
rm -- "$STATE"
echo "A3 원복: $NS/Deployment/$DEPLOY 컨테이너 $CONTAINER 이미지 $image (imagePullPolicy $policy)"
