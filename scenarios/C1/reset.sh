#!/usr/bin/env bash
# reset.sh: C1 — HTTPChaos를 지워 llm-gateway 응답을 되돌리고, llm-gateway를 재시작해 tproxy를 정리한다
#
# Chaos Mesh는 HTTPChaos를 원복할 때 tproxy의 하위 프로세스만 끝내고 tproxy 자체는 Pod가 살아 있는 동안 남긴다.
# 그 pid는 PodHttpChaos status에만 있어서, PodHttpChaos를 지우면 다음 주입이 tproxy를 또 띄우려다
# "process with identifier tproxy-... is running"으로 실패한다. PodHttpChaos를 지우면서(정답 흔적 제거)
# 다시 주입할 수 있게 하려고 대상 Deployment를 재시작해 남은 tproxy를 Pod와 함께 없앤다
set -euo pipefail

NS="${KG_NAMESPACE:-dev}"
CHAOS_NS="${KG_CHAOS_NS:-chaos-mesh}"
NAME=kg-http-2
DEPLOY=llm-gateway

if kubectl -n "$CHAOS_NS" get httpchaos "$NAME" >/dev/null 2>&1; then
  # Chaos Mesh가 Pod의 주입을 걷어낸 뒤에 객체가 사라진다 (finalizer)
  kubectl -n "$CHAOS_NS" delete httpchaos "$NAME" --wait=true --timeout=120s >/dev/null
  echo "C1 원복: $CHAOS_NS/HTTPChaos/$NAME 삭제"
else
  echo "주입 기록($CHAOS_NS/HTTPChaos/$NAME)이 없다."
fi
# Chaos Mesh가 대상 네임스페이스에 만든 Pod별 객체(spec이 빈 채로 남음)도 지운다. 다음 시나리오에서 흔적이 보이지 않게 한다
kubectl -n "$NS" delete podnetworkchaos,podhttpchaos --all --wait=true >/dev/null
echo "${NS}의 PodNetworkChaos·PodHttpChaos 정리"
# 주입 기록이 없어도 재시작한다 (이전 실행이 중간에 끝나 tproxy가 남았을 수 있다)
kubectl -n "$NS" rollout restart "deploy/$DEPLOY" >/dev/null
kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=180s >/dev/null
# rollout status는 새 Pod가 Ready면 끝난다. 이전 Pod가 Terminating으로 남아 있으면 다음 확인·점검에 섞이므로 사라질 때까지 기다린다
for _ in $(seq 60); do
  terminating="$(kubectl -n "$NS" get pods -l "app=$DEPLOY" \
    -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' || true)"
  [[ -z "$terminating" ]] && break
  sleep 2
done
echo "${NS}/Deployment/${DEPLOY} 재시작 완료 (tproxy 정리)"
