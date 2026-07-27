#!/usr/bin/env bash
# One-command setup for zwell-bench
set -euo pipefail
cd "$(dirname "$0")"

echo "==> zwell-bench setup"
python3 -m venv .venv
./.venv/bin/pip -q install -U pip
./.venv/bin/pip -q install -r requirements.txt

BASE="${ZWELL_BASE:-http://127.0.0.1:8889}"
echo
echo "Ready. Point at any OpenAI-compatible server, then:"
echo
echo "  ZWELL_BASE=$BASE ./.venv/bin/python bench_zwell.py --tag my-model"
echo
echo "Thinking models:"
echo "  ZWELL_THINKING=on ZWELL_MAXTOK_MULT=6 ZWELL_BASE=$BASE ./.venv/bin/python bench_zwell.py --tag think"
echo
echo "Example results (no GPU needed): ls results/"
