#!/usr/bin/env bash
# verify_symptom.sh: C4 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. gateway Deployment에 PROXY_TIMEOUT env가 있다 (정상 환경에는 없다)
# 2. gateway Pod는 모두 Ready이고 재시작이 없다 (K8s 상태는 정상)
# 3. 로그인은 성공한다 (짧은 요청)
# 4. 채팅(/agent/invoke)은 gateway에서 504로 실패한다 (긴 요청)
# 5. gateway 로그에 업스트림 시간 초과가 남는다
set -euo pipefail
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18086}
source scripts/target/lib.sh

timeout_env="$(kubectl -n "$TARGET_NS" get deploy/gateway \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="gateway")].env[?(@.name=="PROXY_TIMEOUT")].value}')"
if [[ -z "$timeout_env" ]]; then
  echo "증상 없음: gateway에 PROXY_TIMEOUT env가 없다 (기본값 60초)" >&2
  exit 1
fi

pods="$(kubectl -n "$TARGET_NS" get pods -l app=gateway -o json)"
total="$(jq '.items | length' <<<"$pods")"
ready="$(jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<<"$pods")"
restarts="$(jq '[.items[].status.containerStatuses[]?.restartCount] | add // 0' <<<"$pods")"
if [[ "$total" -lt 1 || "$ready" -ne "$total" || "$restarts" -ne 0 ]]; then
  echo "무효: gateway Pod Ready ${ready}/${total}, 재시작 ${restarts} (전부 Ready이고 재시작 0이어야 한다)" >&2
  exit 1
fi

[ -f "$TEST_ENV" ] || die "${TEST_ENV}가 없다. make seed를 먼저 실행한다"
source "$TEST_ENV"
TOKEN=$(login "$KG_TEST_USER" "$KG_TEST_PASSWORD")  # 실패하면 증상이 아니라 무효 (로그인은 성공해야 한다)
MODEL=${TARGET_MODEL:-gpt-4.1}
org_id=$(api GET /admin/user/me | jq -r '.role.organizations[0].org_id // empty')
[ -n "$org_id" ] || die "테스트 사용자에게 조직이 없다. make seed를 다시 실행한다"

# 채팅 응답이 가끔 시간 제한(1.2초)보다 빨리 끝나 200이 나온다 (2026-11-18 40번 중 1번). 3번 보내 2번 이상 504면 증상으로 본다
N=3
fail=0
started=$(date +%s.%N)
for _ in $(seq "$N"); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/agent/invoke" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "$MODEL" --arg o "$org_id" '{query: "한 문장으로 자기소개를 해 주세요.", model: $m, org_id: $o}')")"
  [[ "$code" == 504 ]] && fail=$((fail + 1))
done
elapsed="$(awk -v s="$started" -v e="$(date +%s.%N)" -v n="$N" 'BEGIN{printf "%.1f", (e - s) / n}')"
if [[ "$fail" -lt 2 ]]; then
  echo "증상 없음: 채팅 ${N}번 중 504가 ${fail}번뿐이다 (PROXY_TIMEOUT=${timeout_env})" >&2
  exit 1
fi

# 로그가 늦게 보일 수 있어 잠시 다시 본다
logged=""
for _ in $(seq 10); do
  glog="$(kubectl -n "$TARGET_NS" logs deploy/gateway --since=10m 2>/dev/null || true)"  # grep -q를 파이프로 받으면 pipefail에서 SIGPIPE로 실패한다
  if grep -q 'UPSTREAM_TIMEOUT.*agent/invoke' <<<"$glog"; then
    logged=1
    break
  fi
  sleep 1
done
if [[ -z "$logged" ]]; then
  echo "무효: gateway 로그에 agent/invoke 업스트림 시간 초과가 없다" >&2
  exit 1
fi

echo "증상 확인: PROXY_TIMEOUT=${timeout_env}, gateway Ready ${ready}/${total} 재시작 ${restarts}, 로그인 성공, 채팅 ${N}번 중 ${fail}번 504 (평균 ${elapsed}초), gateway 로그에 UPSTREAM_TIMEOUT"
