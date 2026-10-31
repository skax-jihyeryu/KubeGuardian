#!/usr/bin/env bash
# 진단 대상 테스트 시드 (F5): 조직·테스트 사용자·LLM 할당. 여러 번 실행해도 같은 결과.
# 전제: make up, LLM 배포가 llm-gateway에 등록됨 (환경 설치 가이드 10단계).
# 테스트 사용자 계정은 .env.test-user(git 제외)에 만들고, 비밀번호는 출력하지 않는다.
cd "$(dirname "$0")/../.."
source scripts/target/lib.sh

ORG=${TEST_ORG:-kg-test}
MODEL=${TARGET_MODEL:-gpt-4.1}

if [ ! -f "$TEST_ENV" ]; then
  (umask 077; printf 'KG_TEST_USER=kg-tester\nKG_TEST_PASSWORD=%s\n' "$(openssl rand -hex 16)" > "$TEST_ENV")
  echo "created $TEST_ENV"
fi
source "$TEST_ENV"

TOKEN=$(login superadmin "$(superadmin_password)")

deployment_id=$(api GET /llm-gateway/deployments | jq -r --arg m "$MODEL" \
  '[.deployment_list[] | select(.model_name == $m and .is_active)][0].id // empty')
[ -n "$deployment_id" ] || die "llm-gateway에 활성 배포 ${MODEL}이 없다. 먼저 등록한다 (환경 설치 가이드 10단계)"

org_id=$(api GET /admin/organization | jq -r --arg n "$ORG" '.organization_list[] | select(.name == $n) | .id' | head -1)
if [ -z "$org_id" ]; then
  org_id=$(api POST /admin/organization "$(jq -n --arg n "$ORG" '{name: $n, description: "KubeGuardian 테스트 조직"}')" | jq -r .id)
fi

user_pk=$(api GET /admin/user | jq -r --arg u "$KG_TEST_USER" '.user_list[] | select(.user_id == $u) | .id' | head -1)
if [ -z "$user_pk" ]; then
  user_pk=$(api POST /admin/user "$(jq -n --arg u "$KG_TEST_USER" --arg pw "$KG_TEST_PASSWORD" \
    '{user_id: $u, username: "KG Tester", company: "KubeGuardian", password: $pw, role: "common"}')" | jq -r .id)
fi

if ! api GET "/admin/organization/$org_id/members" | jq -e --argjson u "$user_pk" '.. | objects | select(.user_id? == $u)' >/dev/null; then
  api POST /admin/organization/members "$(jq -n --argjson o "$org_id" --argjson u "$user_pk" '{org_id: $o, user_id: $u, role: "common"}')" >/dev/null
fi

if ! api GET "/llm-gateway/deployments/assignments/org/$org_id" | jq -e --argjson d "$deployment_id" '.. | objects | select(.deployment_id? == $d)' >/dev/null; then
  api POST /llm-gateway/deployments/assignments "$(jq -n --argjson o "$org_id" --argjson d "$deployment_id" '{org_id: $o, deployment_id: $d}')" >/dev/null
fi

echo "OK org=$ORG(id=$org_id) user=$KG_TEST_USER(id=$user_pk) model=$MODEL(deployment=$deployment_id)"
