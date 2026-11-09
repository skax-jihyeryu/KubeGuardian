#!/usr/bin/env bash
# verify_symptom.sh: A7 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. 주입 기록이 있고 gateway의 AGENT_SERVICE_URL이 주입값(dot 또는 dash)이다
# 2. gateway Pod는 Ready다 (원인이 Pod가 아니라 설정임을 보장)
# 3. front-chat 경유 채팅 스모크가 POST /agent/invoke 5xx로 실패한다 (그 밖의 실패는 환경 문제라 무효)
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/../.."

[[ -f "$STATE" ]] || { echo "무효: A7이 주입되지 않았다 (주입 기록 없음)" >&2; exit 1; }
original="$(sed -n "s/^${ENV_NAME}=//p" "$STATE")"
current="$(current_value)"
variant=""
[[ "$current" == "$(sed "s#://${HOST}:#://${HOST}.:#" <<<"$original")" ]] && variant=dot
[[ "$current" == "$(sed "s#://${HOST}:#://${HOST}-:#" <<<"$original")" ]] && variant=dash
[[ -n "$variant" ]] || { echo "무효: $ENV_NAME=$current 이 주입값이 아니다" >&2; exit 1; }

ready="$(kubectl -n "$NS" get pods -l app=gateway \
  -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c '^True$' || true)"
if [[ "$ready" -eq 0 ]]; then
  echo "무효: gateway Ready Pod가 없다 (다른 원인이 섞임)" >&2
  exit 1
fi

if out="$(bash scripts/target/smoke.sh 2>&1)"; then
  echo "증상 없음: 채팅 스모크가 성공했다 (${out:0:80})" >&2
  exit 1
fi
# 증상은 채팅 호출(POST /agent/invoke)의 5xx다. 환경 문제(.env.test-user 없음·port-forward·로그인 실패)는 증상이 아니다.
if ! grep -qE 'POST /agent/invoke → 5[0-9]{2}' <<<"$out"; then
  echo "무효: 스모크가 채팅 호출 5xx가 아닌 이유로 실패했다 (${out:0:120})" >&2
  exit 1
fi
echo "증상 확인($variant): $ENV_NAME=$current, gateway Ready Pod ${ready}개, 채팅 호출 5xx (${out:0:120})"
