#!/usr/bin/env bash
# verify_symptom.sh: C2 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. 테스트 사용자의 front-chat 경유 로그인이 5xx 또는 시간 초과로 실패한다 (admin이 Redis 세션 저장에서 멈춤)
# 2. admin·redis·gateway·front-chat Pod는 모두 Ready다 (K8s 상태는 정상)
# 3. redis 자체는 응답한다 (redis 장애가 아니라 admin → redis 경로 문제임을 보장)
# 4. admin Pod 안에서 redis:6379 TCP 연결이 실패한다 (원인 증명)
set -euo pipefail
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18083}
source scripts/target/lib.sh

[ -f "$TEST_ENV" ] || die "${TEST_ENV}가 없다. make seed를 먼저 실행한다"
source "$TEST_ENV"

# 게이트웨이 PROXY_TIMEOUT 60초 × 2회 재시도 → 약 120초에 504. 그보다 길게 기다린다
code=$(curl -s -m 150 -o /dev/null -w '%{http_code}' "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg id "$KG_TEST_USER" --arg pw "$KG_TEST_PASSWORD" '{id: $id, password: $pw}')" || true)
# 시간 초과(000)가 port-forward가 끊겨서 난 것이 아닌지 확인한다
kill -0 "$PF_PID" 2>/dev/null || die "무효: port-forward가 끊겼다 (로그인 응답 ${code}를 믿을 수 없다)"
if [[ $code != 5?? && $code != 000 ]]; then
  echo "증상 없음: 로그인 응답 $code (5xx·시간 초과가 아님)" >&2
  exit 1
fi

for app in admin redis gateway front-chat; do
  ready="$(kubectl -n "$TARGET_NS" get pods -l "app=$app" \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c '^True$' || true)"
  if [[ "$ready" -eq 0 ]]; then
    echo "무효: $app Ready Pod가 없다 (다른 원인이 섞임)" >&2
    exit 1
  fi
done

# shellcheck disable=SC2016  # 변수는 redis 컨테이너 안에서 풀린다
pong="$(kubectl -n "$TARGET_NS" exec deploy/redis -- sh -c 'REDISCLI_AUTH="$REDIS_ACCESS_KEY" redis-cli ping' 2>/dev/null || true)"
if [[ "$pong" != PONG ]]; then
  echo "무효: redis 자체가 응답하지 않는다 ($pong)" >&2
  exit 1
fi

# admin 컨테이너의 python으로 Redis Service에 직접 TCP 연결을 시도한다
conn="$(kubectl -n "$TARGET_NS" exec deploy/admin -- python -c "
import socket
try:
    socket.create_connection(('redis', 6379), timeout=3).close()
    print('ok')
except Exception as e:
    print(type(e).__name__)" 2>/dev/null || true)"
if [[ "$conn" == ok || -z "$conn" ]]; then
  echo "증상 없음: admin Pod에서 redis:6379 연결 결과 '${conn:-확인 실패}' (차단되어 있지 않음)" >&2
  exit 1
fi

echo "증상 확인: 로그인 응답 ${code/#000/시간 초과}, admin·redis·gateway·front-chat Ready, redis 자체는 PONG, admin → redis:6379 연결 실패(${conn})"
