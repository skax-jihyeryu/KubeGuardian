#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 scenarios/A6/scenario.py verify

# Pin the shared front-chat port-forward and the subsequent reads to the test context.
kubectl() { command kubectl --context=minikube "$@"; }
TARGET_NS=dev TARGET_SVC=front-chat-svc:80 TARGET_PORT=${TARGET_PORT:-18082}
source scripts/target/lib.sh

probe="kg_a6_probe=$(date +%s)-$$"
code=$(curl -s --max-time 30 -o /dev/null -w '%{http_code}' "$BASE/auth/login?$probe" \
  -H 'Content-Type: application/json' -d '{"id":"kg-verify","password":"x"}') \
  || die "front-chat 연결 실패"
[[ $code == 5?? ]] || die "로그인 응답 $code (5xx가 아님)"

for app in gateway admin; do
  ready=$(kubectl -n "$TARGET_NS" get pods -l "app=$app" \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
    2>/dev/null) || die "$app Pod 상태 조회 실패"
  grep -qx 'True' <<<"$ready" || die "$app Ready Pod가 없다"
done

logs=$(kubectl -n "$TARGET_NS" logs -l app=gateway -c gateway --since=1m --tail=-1 2>/dev/null) \
  || die "gateway 로그 조회 실패"
grep -Fq "업스트림 요청 최종 실패: http://localhost:8001/api/v1/auth/login?$probe" <<<"$logs" \
  || die "해당 로그인 요청의 localhost 업스트림 연결 실패 로그가 없다"
echo "증상 확인: 로그인 응답 $code, gateway와 admin Ready, gateway localhost 연결 실패"
