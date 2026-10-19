#!/usr/bin/env bash
# verify_symptom.sh: A5 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. front-chat 경유 로그인이 5xx로 실패한다 (gateway가 admin을 localhost로 호출)
# 2. gateway Pod는 Ready로 남아 있다 (원인이 Pod가 아니라 설정임을 보장)
set -euo pipefail
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18082}
source scripts/target/lib.sh

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"id":"kg-verify","password":"x"}') || die "front-chat 연결 실패"
if [[ $code != 5?? ]]; then
  echo "증상 없음: 로그인 응답 $code (5xx가 아님)" >&2
  exit 1
fi

ready="$(kubectl -n "$TARGET_NS" get pods -l app=gateway \
  -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c '^True$' || true)"
if [[ "$ready" -eq 0 ]]; then
  echo "무효: gateway Ready Pod가 없다 (다른 원인이 섞임)" >&2
  exit 1
fi

echo "증상 확인: 로그인 응답 $code, gateway Ready Pod ${ready}개"
