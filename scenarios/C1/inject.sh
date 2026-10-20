#!/usr/bin/env bash
# inject.sh: C1 — llm-gateway 채팅 호출이 429(할당량 초과) (Chaos Mesh HTTPChaos)
#
# 템플릿 llm-gateway는 할당량을 강제하지 않아(README "주입 방식") 할당량을 내려도 429가 나지 않는다.
# 대신 llm-gateway Pod의 POST /api/v1/chat/completions 응답을 OpenAI식 할당량 초과 오류(429)로 바꾼다.
# 다른 경로(health·quota·관리 API)와 Pod 상태는 그대로다.
# Chaos 객체는 진단 대상 밖(chaos-mesh 네임스페이스)에 만든다. 대상 네임스페이스에 정답이 보이지 않게 하기 위해서다.
# Chaos Mesh는 대상 Pod 네임스페이스에 PodHttpChaos를 만든다. 주입 중에는 남고, reset.sh가 지운다
#
# 환경 변수
#   KG_NAMESPACE      대상 네임스페이스 (기본 dev)
#   KG_CHAOS_NS       Chaos 객체 네임스페이스 (기본 chaos-mesh)
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME=kg-http-2  # 시나리오 ID를 넣지 않는다 (이름에 정답이 드러나지 않게)

if kubectl -n "$CHAOS_NS" get httpchaos "$NAME" >/dev/null 2>&1; then
  echo "이미 주입된 상태다 ($CHAOS_NS/HTTPChaos/$NAME 있음). reset.sh를 먼저 실행한다." >&2
  exit 1
fi

# OpenAI API가 할당량을 넘었을 때 돌려주는 오류 본문과 같은 모양. HTTPChaos의 replace.body는 base64다
body='{"error":{"message":"You exceeded your current quota, please check your plan and billing details.","type":"insufficient_quota","param":null,"code":"insufficient_quota"}}'
body_b64="$(printf '%s' "$body" | base64 | tr -d '\n')"

kubectl apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: HTTPChaos
metadata:
  name: $NAME
  namespace: $CHAOS_NS
spec:
  mode: all
  selector:
    namespaces: [$NS]
    labelSelectors: {app: llm-gateway}
  target: Response
  port: 8080
  method: POST
  path: /api/v1/chat/completions
  replace:
    code: 429
    headers: {Content-Type: application/json}
    body: $body_b64
EOF

for _ in $(seq 30); do
  injected="$(kubectl -n "$CHAOS_NS" get httpchaos "$NAME" \
    -o jsonpath='{.status.conditions[?(@.type=="AllInjected")].status}' || true)"
  [[ "$injected" == True ]] && break
  sleep 2
done
if [[ "$injected" != True ]]; then
  echo "주입 실패: $CHAOS_NS/HTTPChaos/$NAME AllInjected=$injected" >&2
  exit 1
fi
echo "C1 주입: llm-gateway POST /api/v1/chat/completions 응답 → 429 insufficient_quota ($CHAOS_NS/HTTPChaos/$NAME)"
