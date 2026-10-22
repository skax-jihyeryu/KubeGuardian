#!/usr/bin/env bash
# inject.sh: H1 — 설정 없이도 정상인 서비스(h1-noconfig)를 추가한다 (부재 정상 기준선)
#
# ConfigMap·Secret 참조도 *_SERVICE_URL도 없고, localhost 주소는 같은 Pod 사이드카를 가리킨다.
# 장애가 아니므로 규칙은 아무것도 적발하지 않아야 한다. reset.sh가 지운다.
#
# 환경 변수
#   KG_NAMESPACE   대상 네임스페이스 (기본 dev)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
MANIFEST="$(cd "$(dirname "$0")" && pwd)/manifest.yaml"

if kubectl -n "$NS" get deployment h1-noconfig >/dev/null 2>&1; then
  echo "이미 있다: $NS/Deployment/h1-noconfig. reset.sh를 먼저 실행한다." >&2
  exit 1
fi
kubectl -n "$NS" apply -f "$MANIFEST" >/dev/null
kubectl -n "$NS" rollout status deployment/h1-noconfig --timeout=120s >/dev/null
echo "H1 주입: $NS/Deployment/h1-noconfig, Service/h1-noconfig"
