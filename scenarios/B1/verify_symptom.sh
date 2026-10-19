#!/usr/bin/env bash
# verify_symptom.sh: B1 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. agent Pod마다 직접 /api/v1/health를 부르면 정확히 1개만 500이고 나머지는 200이다
# 2. 모든 agent Pod는 Ready다 (K8s 상태는 정상)
# 3. front-chat 경유 agent 요청을 동시에 N번(기본 36번, 동시 12개) 보내면 일부만 실패한다
#    gateway는 keep-alive 연결을 재사용해 순차 요청은 한 Pod에 몰린다(전부 성공 또는 전부 실패).
#    동시 요청이어야 연결이 여러 개 열려 Pod가 나뉜다
set -euo pipefail
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18084}
source scripts/target/lib.sh
N=${B1_REQUESTS:-36}
CONCURRENCY=${B1_CONCURRENCY:-12}

pods="$(kubectl -n "$TARGET_NS" get pods -l app=agent -o json)"
total="$(jq '.items | length' <<<"$pods")"
ready="$(jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<<"$pods")"
if [[ "$total" -lt 2 || "$ready" -ne "$total" ]]; then
  echo "무효: agent Pod Ready ${ready}/${total} (2개 이상 전부 Ready여야 한다)" >&2
  exit 1
fi

# gateway 컨테이너의 python으로 Pod IP를 직접 부른다 (Service를 거치지 않음)
pod_codes=""
for ip in $(jq -r '.items[].status.podIP' <<<"$pods"); do
  c="$(kubectl -n "$TARGET_NS" exec deploy/gateway -- python -c "
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen('http://$ip:8006/api/v1/health', timeout=5).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)")"
  pod_codes+="$c "
done
bad="$(tr ' ' '\n' <<<"$pod_codes" | grep -c '^500$' || true)"
good="$(tr ' ' '\n' <<<"$pod_codes" | grep -c '^200$' || true)"
if [[ "$bad" -ne 1 || "$good" -ne $((total - 1)) ]]; then
  echo "증상 없음: agent Pod별 응답 ${pod_codes% } (500이 정확히 1개여야 한다)" >&2
  exit 1
fi

[ -f "$TEST_ENV" ] || die "${TEST_ENV}가 없다. make seed를 먼저 실행한다"
source "$TEST_ENV"
TOKEN=$(login "$KG_TEST_USER" "$KG_TEST_PASSWORD")
codes="$(seq "$N" | xargs -P "$CONCURRENCY" -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  "$BASE/agent/health" -H "Authorization: Bearer $TOKEN" || true)"
fail="$(grep -cv '^2' <<<"$codes" || true)"
if [[ "$fail" -eq 0 || "$fail" -eq "$N" ]]; then
  echo "증상 없음: front-chat 경유 agent 요청 ${N}번(동시 ${CONCURRENCY}) 중 ${fail}번 실패 (일부만 실패해야 한다)" >&2
  exit 1
fi

echo "증상 확인: agent Pod별 응답 ${pod_codes% }, front-chat 경유 ${N}번(동시 ${CONCURRENCY}) 중 ${fail}번 실패"
