#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 scenarios/C8/scenario.py inject
