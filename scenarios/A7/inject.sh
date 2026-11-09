#!/usr/bin/env bash
# inject.sh: A7 — gateway의 AGENT_SERVICE_URL 호스트 끝에 점(.) 또는 대시(-) 한 글자를 더한다
#   http://agent:8006 → http://agent.:8006 (KG_TYPO=dot) / http://agent-:8006 (KG_TYPO=dash)
# 다른 env는 건드리지 않는다. 원래 값은 .state/(커밋하지 않음)에 저장해 reset.sh가 되돌린다.
# 주입 중 재주입은 거절한다.
source "$(dirname "$0")/lib.sh"

if [[ -f "$STATE" ]]; then
  echo "이미 주입된 상태다 ($STATE 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi
original="$(current_value)"
if [[ "$original" != "http://${HOST}:"* ]]; then
  echo "${NS}/Deployment/${DEPLOY}의 ${ENV_NAME}이 예상 형식(http://${HOST}:<포트>)이 아니다: ${original:-없음}" >&2
  exit 1
fi
injected="$(typo_value "$original")"

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$ENV_NAME=$original" > "$STATE"
kubectl -n "$NS" set env "deployment/$DEPLOY" "$ENV_NAME=$injected" >/dev/null
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=180s >/dev/null
echo "A7 주입($TYPO): $NS/Deployment/$DEPLOY $ENV_NAME $original → $injected"
