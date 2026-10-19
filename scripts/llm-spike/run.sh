#!/usr/bin/env bash
# 진단용 Azure OpenAI를 minikube Pod 안에서 호출해 본다 (이슈 #8 스파이크).
# 필요: 레포 루트의 .env (AZURE_OPENAI_*), ~/.minikube/certs/sk-ca.pem (환경 설치 가이드 §2)
# 키 값은 화면에 출력하지 않는다.
set -euo pipefail
MODEL=${1:-gpt-4.1}
TEMP=${2:-}  # temperature 0을 거부하는 모델이면 none
cd "$(dirname "$0")/../.."
NS=kubeguardian

kubectl -n "$NS" create secret generic kg-azure-openai --from-env-file=<(grep -E '^(LLM_PROVIDER|AZURE_OPENAI_[A-Z_]+)=' .env) \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create configmap kg-ca-bundle --from-file=sk-ca.pem="$HOME/.minikube/certs/sk-ca.pem" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create configmap llm-spike-code \
  --from-file=core-init.py=app/core/__init__.py --from-file=app/core/config.py --from-file=app/core/llm.py \
  --from-file=scripts/llm-spike/llm_smoke.py --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" delete pod llm-spike --ignore-not-found >/dev/null
sed "s|python /work/llm_smoke.py|python /work/llm_smoke.py $MODEL $TEMP|" scripts/llm-spike/pod.yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/llm-spike --timeout=180s \
  || kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Failed pod/llm-spike --timeout=5s || true
kubectl -n "$NS" logs llm-spike
