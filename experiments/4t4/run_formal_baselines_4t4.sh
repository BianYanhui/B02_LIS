#!/usr/bin/env bash
# Run the full frozen baseline matrix.  The manifest must exist first.
set -euo pipefail
ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/experiments/4t4/run_formal4t4.py"
OUT="$ROOT/analysis/formal4t4"
MANIFEST=${1:?usage: run_formal_baselines_4t4.sh <frozen_manifest.json>}
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }
KV=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["vllm"]["kv_cache_tokens_per_instance"])' "$MANIFEST")
BURST=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["ratefifo_burst_frames"])' "$MANIFEST")
COMMON=(--out-dir "$OUT" --frozen-manifest "$MANIFEST" --instances 4 --repetitions 5 --n-requests 120 --warmup 24 --concurrency 4 --output-tokens 4 --kv-cache-tokens "$KV" --rhos 0.5,0.8,1.0,1.2 --policies FullSync,RateFIFO,LatestOnly,AgeCov-Greedy,StaticSemantic,Adaptive --global-topk 16 --relay-max-inflight 4 --rate-burst-frames "$BURST" --cooldown-s 1.0)
"$PY" "$HARNESS" "${COMMON[@]}" --stage baseline --tag baseline_original_20260726 --seed 2026072600 --workload original_compatible --pool-size 32 --overlap 0.0 --alpha 1.2 --steps 3
"$PY" "$HARNESS" "${COMMON[@]}" --stage baseline --tag baseline_reuse_20260726 --seed 2026072600 --workload reuse_intensive --pool-size 64 --overlap 0.25 --alpha 1.2 --steps 3
