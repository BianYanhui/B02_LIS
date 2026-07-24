#!/usr/bin/env bash
# Run one low -> high -> low shared-link experiment using GPUs 0--2 only.
# The live harness owns request generation and state signaling.  This wrapper
# changes only the real tc HTB signaling service rate after the link cell has
# begun, and writes phase timestamps for post-hoc per-phase AoI/TTFT analysis.
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/shared_link_exp/run_live_shared_link_v3.py"
RATE="$ROOT/shared_link_exp/net/cell_rate.sh"
OUT="$ROOT/shared_link_exp/live_v3/results"

POLICY=${1:?usage: run_dynamic_step_v1.sh <agg_static|agg_full> <tag> <seed>}
TAG=${2:?usage: run_dynamic_step_v1.sh <agg_static|agg_full> <tag> <seed>}
SEED=${3:?usage: run_dynamic_step_v1.sh <agg_static|agg_full> <tag> <seed>}

case "$POLICY" in
  agg_static|agg_full) ;;
  *) echo "policy must be agg_static or agg_full" >&2; exit 2;;
esac

LOG="$OUT/dynamic_${TAG}.log"
EVENTS="$OUT/dynamic_events_${TAG}.jsonl"
if [[ -e "$LOG" || -e "$EVENTS" ]]; then
  echo "refusing to overwrite existing dynamic evidence for tag=$TAG" >&2
  exit 3
fi

# Keep enough measured requests in every rate interval.  The previous
# 24-request warm-up plus a 30 s first dwell could consume the entire
# initial low-rate period, yielding no dispatch-time low-phase samples.
"$PY" "$HARNESS" \
  --tag "$TAG" --seed "$SEED" --instances 3 --repetitions 1 \
  --n-requests 96 --warmup 12 --pool-size 32 --alpha 1.2 --steps 3 \
  --concurrency 3 --output-tokens 4 --kv-cache-tokens 50000 \
  --rhos 0.5 --policies "$POLICY" --global-topk 16 \
  --relay-max-inflight 2 --cooldown-s 0.5 >"$LOG" 2>&1 &
HARNESS_PID=$!

abort() {
  kill "$HARNESS_PID" 2>/dev/null || true
  wait "$HARNESS_PID" 2>/dev/null || true
}
trap abort INT TERM

while ! grep -q "cell rate set: link=" "$LOG"; do
  if ! kill -0 "$HARNESS_PID" 2>/dev/null; then
    wait "$HARNESS_PID"
    exit $?
  fi
  sleep 0.25
done

# The harness begins the link cell at rho=0.5.  Preserve that measured low
# rate, then apply rho=1.2 for the middle phase (low_rate * 0.5 / 1.2).
LOW_RATE=$(sed -nE 's/.*cell rate set: link=([0-9]+)bit.*/\1/p' "$LOG" | tail -1)
[[ -n "$LOW_RATE" ]] || { echo "could not parse initial low rate" >&2; abort; exit 4; }
HIGH_RATE=$((LOW_RATE * 5 / 12))
[[ "$HIGH_RATE" -ge 64 ]] || HIGH_RATE=64

stamp() {
  local phase=$1 rate=$2
  printf '{"timestamp_unix":%s,"phase":"%s","sig_bit_per_s":%s,"policy":"%s","tag":"%s"}\n' \
    "$(date +%s.%N)" "$phase" "$rate" "$POLICY" "$TAG" >>"$EVENTS"
}

stamp low_initial "$LOW_RATE"
sleep 45
bash "$RATE" --sig-bit "$HIGH_RATE" >>"$LOG" 2>&1
stamp high "$HIGH_RATE"
sleep 45
bash "$RATE" --sig-bit "$LOW_RATE" >>"$LOG" 2>&1
stamp low_recovery "$LOW_RATE"

wait "$HARNESS_PID"
trap - INT TERM
