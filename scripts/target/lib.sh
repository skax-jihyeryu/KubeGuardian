# 진단 대상(dev) API 호출 공용 함수. seed.sh·smoke.sh가 source한다.
# Service(기본 gateway)를 임시 port-forward로 열고, 끝나면 닫는다. 비밀값·토큰은 출력하지 않는다.
set -euo pipefail

TARGET_DIR=${TARGET_DIR:-$HOME/Cursor/kg-target}
TARGET_NS=${TARGET_NS:-dev}
PORT=${TARGET_PORT:-18080}
SVC=${TARGET_SVC:-gateway:8000}  # <Service>:<port>. smoke.sh는 front-chat을 거친다
BASE="http://localhost:$PORT/api/v1"
TEST_ENV=${TEST_ENV:-.env.test-user}

kubectl -n "$TARGET_NS" port-forward "svc/${SVC%%:*}" "$PORT:${SVC##*:}" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null' EXIT
for _ in $(seq 20); do curl -s -o /dev/null "localhost:$PORT" && break; sleep 0.5; done

die() { echo "FAIL $*" >&2; exit 1; }

# login <id> <password> → access token
login() {
  local token
  token=$(curl -s "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg id "$1" --arg pw "$2" '{id: $id, password: $pw}')" | jq -r '.access_token // empty')
  [ -n "$token" ] || die "로그인 실패: $1"
  echo "$token"
}

# api <METHOD> <path> [json] → 응답 본문 (HTTP 4xx·5xx면 실패)
api() {
  local out code
  out=$(curl -s -w '\n%{http_code}' -X "$1" "$BASE$2" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' ${3:+-d "$3"})
  code=$(tail -n1 <<<"$out")
  [ "$code" -lt 400 ] || die "$1 $2 → $code $(sed '$d' <<<"$out" | jq -c 'del(.. | .access_key?, .password?)' 2>/dev/null | cut -c1-300)"
  sed '$d' <<<"$out"
}

# 템플릿 backend-admin이 기동할 때 만드는 superadmin의 초기 비밀번호 (진단 대상 로컬 전용 값)
superadmin_password() {
  sed -nE 's/.*password="([^"]*)".*S106.*/\1/p' "$TARGET_DIR/backend-admin/app/main.py" | head -1
}
