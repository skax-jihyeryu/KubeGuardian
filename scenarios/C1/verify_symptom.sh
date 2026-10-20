#!/usr/bin/env bash
# verify_symptom.sh: C1 — 증상 발생 확인. 실패하면 그 실행은 무효
#
# 1. llm-gateway·agent·gateway·front-chat Pod는 모두 Ready다 (K8s 상태는 정상)
# 2. 테스트 사용자의 front-chat 경유 로그인은 성공한다 (로그인 경로는 정상)
# 3. 채팅(/agent/invoke)은 HTTP 200인데 answer가 "오류가 발생했습니다: LLM Gateway error: 429"로 시작한다
#    (템플릿 agent는 LLM 호출이 실패해도 200에 오류 문구를 answer로 돌려준다)
# 4. agent Pod에서 llm-gateway의 /api/v1/chat/completions를 직접 부르면 429다 (원인 증명)
set -euo pipefail
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18085}
source scripts/target/lib.sh
MODEL=${TARGET_MODEL:-gpt-4.1}
EXPECTED="오류가 발생했습니다: LLM Gateway error: 429"

for app in llm-gateway agent gateway front-chat; do
  pods="$(kubectl -n "$TARGET_NS" get pods -l "app=$app" -o json)"
  total="$(jq '.items | length' <<<"$pods")"
  ready="$(jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' <<<"$pods")"
  if [[ "$total" -eq 0 || "$ready" -ne "$total" ]]; then
    echo "무효: $app Pod Ready ${ready}/${total} (전부 Ready여야 한다. 다른 원인이 섞임)" >&2
    exit 1
  fi
done

[ -f "$TEST_ENV" ] || die "${TEST_ENV}가 없다. make seed를 먼저 실행한다"
source "$TEST_ENV"
TOKEN=$(login "$KG_TEST_USER" "$KG_TEST_PASSWORD")
org_id=$(api GET /admin/user/me | jq -r '.role.organizations[0].org_id // empty')
[ -n "$org_id" ] || die "테스트 사용자에게 조직이 없다. make seed를 다시 실행한다"

answer=$(api POST /agent/invoke "$(jq -n --arg m "$MODEL" --arg o "$org_id" \
  '{query: "한 문장으로 자기소개를 해 주세요.", model: $m, org_id: $o}')" | jq -r '.data.answer // empty')
if [[ "$answer" != "$EXPECTED"* ]]; then
  echo "증상 없음: 채팅 answer '${answer:0:80}' ('${EXPECTED}'로 시작해야 한다)" >&2
  exit 1
fi

# agent 컨테이너의 python으로 llm-gateway를 직접 부른다. 본문은 비워 보낸다 (정상이면 401, 주입 중이면 429)
# 다른 경로(GET /docs)는 200이어야 한다 (llm-gateway 전체 장애가 아니라 채팅 완성 응답만 429)
codes="$(kubectl -n "$TARGET_NS" exec deploy/agent -- python -c "
import urllib.request, urllib.error
def call(req):
    try:
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0
base = 'http://llm-gateway-svc:8080'
chat = urllib.request.Request(base + '/api/v1/chat/completions', data=b'{}',
                              headers={'Content-Type': 'application/json'}, method='POST')
print(call(chat), call(base + '/docs'))" 2>/dev/null || true)"
read -r code other <<<"$codes"
if [[ "${code:-}" != 429 ]]; then
  echo "증상 없음: agent → llm-gateway /api/v1/chat/completions 직접 호출 응답 ${code:-확인 실패} (429여야 한다)" >&2
  exit 1
fi
if [[ "${other:-}" != 200 ]]; then
  echo "무효: llm-gateway GET /docs 응답 ${other:-확인 실패} (200이어야 한다. 채팅 경로 밖 장애가 섞임)" >&2
  exit 1
fi

echo "증상 확인: llm-gateway·agent·gateway·front-chat Ready, 로그인 성공, 채팅 answer '${answer:0:60}', agent → llm-gateway 직접 호출 429 (GET /docs는 200)"
