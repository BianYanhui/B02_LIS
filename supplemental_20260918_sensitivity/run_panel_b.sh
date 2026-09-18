#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/byh/B02
OUT="$ROOT/supplemental_20260918_sensitivity"
cd "$ROOT"
KV="${1:?usage: run_panel_b.sh KV_CACHE_TOKENS PREFIX_LENGTH [TAG]}"
LEN="${2:?}"
TAG="${3:-panel_b_L${LEN}}"
source "$ROOT/poc/.venv/bin/activate"
exec python supplemental_20260918_sensitivity/run_sensitivity.py \
  --stage panel_b --tag "$TAG" --out-dir "$OUT" \
  --workload reuse_intensive --rhos 1.2 --repetitions 5 --seed 20260726 \
  --rate-burst-frames 1 --prefix-length "$LEN" --kv-cache-tokens "$KV"
