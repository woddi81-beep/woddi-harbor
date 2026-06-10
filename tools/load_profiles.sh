#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:9680}"
PYTHON="${PYTHON:-.venv/bin/python}"

"$PYTHON" tools/load_test.py --url "$BASE_URL/api/health" --requests 10000 --concurrency 64
"$PYTHON" tools/load_test.py --url "$BASE_URL/api/ready" --requests 5000 --concurrency 32
