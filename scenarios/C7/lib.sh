#!/usr/bin/env bash
# C7 공용: 안전 검사와 주입값 계산. inject.sh·reset.sh·verify_symptom.sh가 source한다.
#
# 환경 변수
#   KG_NAMESPACE   대상 네임스페이스 (dev만 허용)
#   KG_TYPO        dot(기본): 호스트 끝에 점, dash: 호스트 끝에 대시
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY=gateway
ENV_NAME=AGENT_SERVICE_URL
HOST=agent
TYPO="${KG_TYPO:-dot}"
STATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.state/$NS-$DEPLOY.env"

if [[ "$NS" != "dev" ]]; then
  echo "C7은 테스트 네임스페이스 dev에서만 쓴다 (KG_NAMESPACE=$NS)" >&2
  exit 1
fi
ctx="$(kubectl config current-context)"
if [[ "$ctx" != "minikube" ]]; then
  echo "C7은 kube context minikube에서만 쓴다 (현재 $ctx)" >&2
  exit 1
fi
case "$TYPO" in
  dot) SUFFIX="." ;;
  dash) SUFFIX="-" ;;
  *) echo "KG_TYPO는 dot 또는 dash여야 한다 (받은 값: $TYPO)" >&2; exit 1 ;;
esac

# 호스트(agent)만 바꾼다. 스킴·포트는 그대로 둔다.
typo_value() { sed "s#://${HOST}:#://${HOST}${SUFFIX}:#" <<<"$1"; }
current_value() {
  kubectl -n "$NS" set env "deployment/$DEPLOY" --list | sed -n "s/^${ENV_NAME}=//p"
}
