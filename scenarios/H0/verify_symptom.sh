#!/usr/bin/env bash
# verify_symptom.sh: H0 — "증상"은 정상 동작이다. front-chat 경유 로그인 → 채팅이 성공해야 한다
# 평가 러너는 다른 시나리오를 원복한 뒤에도 이 스크립트로 복구를 확인한다.
set -euo pipefail
exec bash "$(dirname "$0")/../../scripts/target/smoke.sh"
