#!/usr/bin/env bash
# Exercise the already-patched vLLM owner ValidateAndPin implementation on all
# four formal endpoints.  This is a complementary owner-safety check, run
# after serving measurements because it intentionally injects eviction and a
# restart-epoch transition into each endpoint's live BlockPool.
#
# Usage: run_native_validation_matrix_4t4.sh
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
BENCH="$ROOT/supplemental_20260717/run_vllm_native_pin_microbench_v6.py"
OUT="$ROOT/analysis/formal4t4/raw/native_validation"

for GPU in 0 1 2 3; do
  DEST="$OUT/gpu${GPU}"
  [[ ! -e "$DEST" ]] || { echo "refusing to overwrite $DEST" >&2; exit 2; }
  "$PY" "$BENCH" --base-url "http://127.0.0.1:$((8000 + GPU))" \
    --out-dir "$DEST" --workers 8 --ops-per-worker 16 --invalid-operations 32 \
    --eviction-attempts 64 --timeout-s 30
done
