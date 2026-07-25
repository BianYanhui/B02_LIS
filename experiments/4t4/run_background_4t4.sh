#!/usr/bin/env bash
# Run a frozen 4T4 background-traffic cell set.
# Usage: run_background_4t4.sh <FullSync|RateFIFO|Adaptive> <none|medium|high> <tag> <seed> <manifest>
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/experiments/4t4/run_formal4t4.py"
OUT="$ROOT/analysis/formal4t4"
POLICY=${1:?missing policy}
LEVEL=${2:?missing background level}
TAG=${3:?missing tag}
SEED=${4:?missing seed}
MANIFEST=${5:?missing manifest}

case "$POLICY" in FullSync|RateFIFO|Adaptive) ;; *) echo "unsupported policy" >&2; exit 2;; esac
case "$LEVEL" in
  none) BG_ARGS=();;
  medium) BG_ARGS=(--background --background-rate 1000);;
  high) BG_ARGS=(--background --background-rate 10000);;
  *) echo "background level must be none, medium, or high" >&2; exit 2;;
esac
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }
BURST=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["ratefifo_burst_frames"])' "$MANIFEST")

exec "$PY" "$HARNESS" --out-dir "$OUT" --stage background --tag "$TAG" --seed "$SEED" \
  --frozen-manifest "$MANIFEST" --instances 4 --repetitions 5 --workload reuse_intensive \
  --n-requests 120 --warmup 24 --pool-size 64 --overlap 0.25 --alpha 1.2 --steps 3 \
  --concurrency 4 --output-tokens 4 --kv-cache-tokens 104544 --rhos 1.0 --policies "$POLICY" \
  --global-topk 16 --relay-max-inflight 4 --rate-burst-frames "$BURST" --cooldown-s 1.0 "${BG_ARGS[@]}"
