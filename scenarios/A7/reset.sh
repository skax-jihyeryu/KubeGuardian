#!/usr/bin/env bash
# reset.sh: A7 — 저장된 원래 AGENT_SERVICE_URL로 되돌린다.
# 현재 값이 저장된 원래 값이거나 주입값(dot·dash)일 때만 되돌린다. 그 밖의 값이면 덮어쓰지 않고 중단한다.
source "$(dirname "$0")/lib.sh"

if [[ ! -f "$STATE" ]]; then
  echo "주입 기록($STATE)이 없다. 되돌릴 것이 없다."
  exit 0
fi
original="$(sed -n "s/^${ENV_NAME}=//p" "$STATE")"
current="$(current_value)"
allowed=false
for s in . -; do
  [[ "$current" == "$(sed "s#://${HOST}:#://${HOST}${s}:#" <<<"$original")" ]] && allowed=true
done
[[ "$current" == "$original" ]] && allowed=true
if ! $allowed; then
  echo "현재 $ENV_NAME=$current 은 원래 값도 A7 주입값도 아니다. 덮어쓰지 않고 중단한다 (기록: $STATE)" >&2
  exit 1
fi

kubectl -n "$NS" set env "deployment/$DEPLOY" "$ENV_NAME=$original" >/dev/null
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=180s >/dev/null
rm -- "$STATE"
echo "A7 원복: $NS/Deployment/$DEPLOY $ENV_NAME=$original"
