#!/usr/bin/env bash
# Calibration only. Outputs remain under analysis/formal4t4/raw/calibration
# and are explicitly excluded from paper aggregates.
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/experiments/4t4/run_formal4t4.py"
OUT="$ROOT/analysis/formal4t4"
KV=104544
BASE=(--out-dir "$OUT" --stage calibration --instances 4 --repetitions 1 --n-requests 48 --warmup 12 --workload reuse_intensive --pool-size 32 --overlap 0.25 --alpha 1.2 --steps 3 --output-tokens 4 --kv-cache-tokens "$KV" --global-topk 16 --relay-max-inflight 4 --cooldown-s 0.5)

# Concurrency calibration, not used for paper aggregation. Both FullSync and
# Adaptive see the same trace at rho=1.0 in every trial.
for C in 1 2 4 8; do
  "$PY" "$HARNESS" "${BASE[@]}" --tag "calib_concurrency${C}_20260726" --seed $((2026072610 + C)) --concurrency "$C" --rhos 1.0 --policies FullSync,Adaptive --rate-burst-frames 4
done

# Fair RateFIFO burst selection. The three candidates use the exact same
# physical rho grid and trace generator; all rows are calibration-only.
for B in 1 4 16; do
  "$PY" "$HARNESS" "${BASE[@]}" --tag "calib_ratefifo_burst${B}_20260726" --seed 2026072620 --concurrency 4 --rhos 0.8,1.0,1.2 --policies RateFIFO --rate-burst-frames "$B"
done

# rho and overlap diagnostics establish low/moderate/saturated ranges and
# cross-instance redundancy without entering the formal aggregate.
"$PY" "$HARNESS" "${BASE[@]}" --tag calib_rho_20260726 --seed 2026072630 --concurrency 4 --rhos 0.4,0.6,0.8,1.0,1.2 --policies FullSync,Adaptive --rate-burst-frames 4
for O in 0.0 0.25 0.5 0.75; do
  "$PY" "$HARNESS" --out-dir "$OUT" --stage calibration --tag "calib_overlap${O}_20260726" --seed 2026072640 --instances 4 --repetitions 1 --n-requests 48 --warmup 12 --workload reuse_intensive --pool-size 32 --overlap "$O" --alpha 1.2 --steps 3 --concurrency 4 --output-tokens 4 --kv-cache-tokens "$KV" --rhos 1.0 --policies FullSync,Adaptive --global-topk 16 --relay-max-inflight 4 --rate-burst-frames 4 --cooldown-s 0.5
done
