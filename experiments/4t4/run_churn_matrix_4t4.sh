#!/usr/bin/env bash
# High-churn stale-hint experiment. The 75% physical replica preseed plus a
# 64-prefix reuse-intensive pool exceeds the measured per-GPU cache working
# set; the existing LRU shadow therefore emits eviction tombstones into the
# same TCP/tc path while physical cached-token telemetry detects fallbacks.
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/experiments/4t4/run_formal4t4.py"
OUT="$ROOT/analysis/formal4t4"
MANIFEST=${1:?usage: run_churn_matrix_4t4.sh <frozen_manifest.json>}
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }
KV=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["vllm"]["kv_cache_tokens_per_instance"])' "$MANIFEST")
BURST=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["ratefifo_burst_frames"])' "$MANIFEST")

for POLICY in FullSync RateFIFO LatestOnly Adaptive; do
  "$PY" "$HARNESS" --out-dir "$OUT" --stage churn --tag "churn_${POLICY}_20260726" --seed 2026072900 \
    --frozen-manifest "$MANIFEST" --instances 4 --repetitions 5 --workload reuse_intensive \
    --n-requests 120 --warmup 24 --pool-size 64 --overlap 0.75 --alpha 1.4 --steps 3 \
    --concurrency 4 --output-tokens 4 --kv-cache-tokens "$KV" --rhos 1.2 --policies "$POLICY" \
    --global-topk 16 --relay-max-inflight 4 --rate-burst-frames "$BURST" --cooldown-s 1.0
done
