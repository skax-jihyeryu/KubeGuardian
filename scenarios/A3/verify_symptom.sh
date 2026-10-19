#!/usr/bin/env bash
# verify_symptom.sh: A3 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. 지금 Deployment가 가리키는 이미지로 뜬 새 Pod가 이미지 받기 실패(ErrImagePull·ImagePullBackOff)로 대기 중이다
# 2. 이전 버전 Pod는 replicas만큼 Ready로 남아 있다 (사용자 영향 없음, 다른 원인이 섞이지 않음)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY="${A3_DEPLOY:-agent}"
CONTAINER="${A3_CONTAINER:-$DEPLOY}"
POD_SELECTOR="${A3_POD_SELECTOR:-app=$DEPLOY}"

dep="$(kubectl -n "$NS" get deployment "$DEPLOY" -o json)"
image="$(jq -r --arg c "$CONTAINER" '.spec.template.spec.containers[] | select(.name == $c) | .image' <<<"$dep")"
replicas="$(jq -r '.spec.replicas' <<<"$dep")"
pods="$(kubectl -n "$NS" get pods -l "$POD_SELECTOR" -o json)"

waiting="$(jq -r --arg c "$CONTAINER" --arg i "$image" '
  [.items[] | .status.containerStatuses[]? | select(.name == $c and .image == $i)
   | .state.waiting.reason // empty | select(. == "ErrImagePull" or . == "ImagePullBackOff")] | length' <<<"$pods")"
if [[ "$waiting" -eq 0 ]]; then
  echo "증상 없음: $NS/Deployment/$DEPLOY 이미지 ${image}로 이미지 받기 실패 중인 Pod가 없다" >&2
  exit 1
fi

ready="$(jq -r '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<<"$pods")"
if [[ "$ready" -lt "$replicas" ]]; then
  echo "무효: $POD_SELECTOR Ready Pod ${ready}개 < replicas ${replicas} (다른 원인이 섞임)" >&2
  exit 1
fi

echo "증상 확인: 이미지 $image 받기 실패 Pod ${waiting}개, 이전 버전 Ready Pod ${ready}/${replicas}개"
