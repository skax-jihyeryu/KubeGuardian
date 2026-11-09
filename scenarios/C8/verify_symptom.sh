#!/usr/bin/env bash
# 정상에서는 실패해야 한다. 주입 후 DB·Pod Ready·실제 호출 오류·서버 로그를 확인한다.
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 scenarios/C8/scenario.py verify
