#!/usr/bin/env bash
# 진단 대상 E2E 스모크: front-chat 경유 로그인 → 채팅 (H0 정상 기준선 확인).
# 전제: make seed. 성공하면 OK와 응답 앞부분을 출력하고, 실패하면 FAIL과 함께 0이 아닌 값으로 끝난다.
cd "$(dirname "$0")/../.."
TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18081}
source scripts/target/lib.sh

[ -f "$TEST_ENV" ] || die "${TEST_ENV}가 없다. make seed를 먼저 실행한다"
source "$TEST_ENV"
MODEL=${TARGET_MODEL:-gpt-4.1}

TOKEN=$(login "$KG_TEST_USER" "$KG_TEST_PASSWORD")
org_id=$(api GET /admin/user/me | jq -r '.role.organizations[0].org_id // empty')
[ -n "$org_id" ] || die "테스트 사용자에게 조직이 없다. make seed를 다시 실행한다"

started=$(date +%s)
answer=$(api POST /agent/invoke "$(jq -n --arg m "$MODEL" --arg o "$org_id" \
  '{query: "한 문장으로 자기소개를 해 주세요.", model: $m, org_id: $o}')" | jq -r '.data.answer // empty')
[ -n "$answer" ] || die "채팅 응답이 비어 있다"
# 템플릿 agent는 LLM 호출이 실패해도 HTTP 200에 이 문구를 answer로 돌려준다
[[ "$answer" != 오류가\ 발생했습니다* ]] || die "$answer"
echo "OK $(( $(date +%s) - started ))s model=$MODEL answer=${answer:0:80}"
