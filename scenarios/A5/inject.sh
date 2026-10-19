#!/usr/bin/env bash
# inject.sh: A5 — gateway 서비스 주소 설정 누락 (원본 템플릿 상태 = 기획서 §2.3 T1)
#
# gateway의 하위 서비스 주소 env(F1)를 코드 기본값인 localhost 주소로 바꾼다.
# env가 없을 때와 앱 동작은 같다(코드 기본값이 같은 localhost 주소). env를 지우지 않고 값을 남기는 것은
# S04가 "값이 localhost를 가리킴"을 판정하는 선언 규칙이기 때문이다 (값이 없으면 판정할 대상이 없다).
# 원래 값은 진단 대상 밖(이 폴더의 .state/)에 저장해 reset.sh가 되돌린다.
#
# 환경 변수
#   KG_NAMESPACE   대상 네임스페이스 (기본 dev)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
DEPLOY=gateway
STATE="$(cd "$(dirname "$0")" && pwd)/.state/$NS-$DEPLOY.env"
# 템플릿 backend-gateway core/config.py의 기본값
DEFAULTS=(
  ADMIN_SERVICE_URL=http://localhost:8001
  AUTH_VERIFY_URL=http://localhost:8001/api/v1/auth/verify
  AGENT_SERVICE_URL=http://localhost:8006
  LLM_GATEWAY_SERVICE_URL=http://localhost:8080
)

if [[ -f "$STATE" ]]; then
  echo "이미 주입된 상태다 ($STATE 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

current="$(kubectl -n "$NS" set env "deployment/$DEPLOY" --list)"
original=()
for kv in "${DEFAULTS[@]}"; do
  line="$(grep -E "^${kv%%=*}=" <<<"$current" || true)"
  if [[ -z "$line" ]]; then
    echo "$NS/Deployment/$DEPLOY에 ${kv%%=*} env가 없다. F1이 적용된 상태에서 주입한다." >&2
    exit 1
  fi
  original+=("$line")
done

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "${original[@]}" > "$STATE"
kubectl -n "$NS" set env "deployment/$DEPLOY" "${DEFAULTS[@]}" >/dev/null
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=180s >/dev/null
echo "A5 주입: $NS/Deployment/$DEPLOY 서비스 주소 env ${#DEFAULTS[@]}개 → localhost"
