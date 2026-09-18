#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/byh/B02
OUT="$ROOT/supplemental_20260918_sensitivity"
cd "$ROOT"
KV="${1:?usage: run_panel_a.sh KV_CACHE_TOKENS}"
source "$ROOT/poc/.venv/bin/activate"
exec python supplemental_20260918_sensitivity/run_sensitivity.py \
  --stage panel_a --tag panel_a --out-dir "$OUT" \
  --workload reuse_intensive --rhos 1.2 --repetitions 5 --seed 20260726 \
  --rate-burst-frames 1 --kv-cache-tokens "$KV"
