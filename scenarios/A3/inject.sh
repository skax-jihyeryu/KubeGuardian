#!/usr/bin/env bash
# inject.sh: A3 — agent 신규 배포를 없는 이미지 tag로
#
# agent 컨테이너 이미지를 레지스트리에 없는 tag(기본 kg-target/agent:kg-a3-missing)로 바꾼다.
# 로컬 이미지는 imagePullPolicy: Never로 넣어 두었기 때문에 그대로 두면 사유가 ErrImageNeverPull이 된다.
# 실제 클러스터에서 "없는 tag"는 레지스트리에서 받다가 실패(ErrImagePull·ImagePullBackOff)하므로
# imagePullPolicy도 IfNotPresent로 바꿔 레지스트리(docker.io)에서 받게 한다.
# 원래 값은 진단 대상 밖(이 폴더의 .state/)에 저장해 reset.sh가 되돌린다.
#
# 환경 변수
#   KG_NAMESPACE   대상 네임스페이스 (기본 dev)
#   A3_DEPLOY      대상 Deployment (기본 agent)
#   A3_CONTAINER   대상 컨테이너 (기본 Deployment 이름과 같음)
#   A3_IMAGE       바꿀 이미지 (기본 kg-target/<컨테이너>:kg-a3-missing)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY="${A3_DEPLOY:-agent}"
CONTAINER="${A3_CONTAINER:-$DEPLOY}"
IMAGE="${A3_IMAGE:-kg-target/$CONTAINER:kg-a3-missing}"
STATE="$(cd "$(dirname "$0")" && pwd)/.state/$NS-$DEPLOY.image"

if [[ -f "$STATE" ]]; then
  echo "이미 주입된 상태다 ($STATE 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

jp="{.spec.template.spec.containers[?(@.name==\"$CONTAINER\")]"
image="$(kubectl -n "$NS" get deployment "$DEPLOY" -o jsonpath="$jp.image}")"
policy="$(kubectl -n "$NS" get deployment "$DEPLOY" -o jsonpath="$jp.imagePullPolicy}")"
if [[ -z "$image" ]]; then
  echo "$NS/Deployment/${DEPLOY}에 컨테이너 ${CONTAINER}가 없다." >&2
  exit 1
fi

mkdir -p "$(dirname "$STATE")"
printf '%s\n%s\n' "$image" "$policy" > "$STATE"
kubectl -n "$NS" patch deployment "$DEPLOY" --type strategic -p "$(jq -n \
  --arg c "$CONTAINER" --arg i "$IMAGE" \
  '{spec: {template: {spec: {containers: [{name: $c, image: $i, imagePullPolicy: "IfNotPresent"}]}}}}')" >/dev/null
echo "A3 주입: $NS/Deployment/$DEPLOY 컨테이너 $CONTAINER 이미지 $image → $IMAGE"
