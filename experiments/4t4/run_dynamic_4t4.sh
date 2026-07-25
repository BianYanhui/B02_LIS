#!/usr/bin/env bash
# Execute one frozen low -> high -> low capacity trace for a 4T4 policy.
# Usage: run_dynamic_4t4.sh <RateFIFO|LatestOnly|StaticSemantic|Adaptive> <tag> <seed> <manifest>
set -euo pipefail

ROOT=/home/byh/B02
PY="$ROOT/poc/.venv/bin/python"
HARNESS="$ROOT/experiments/4t4/run_formal4t4.py"
RATE="$ROOT/experiments/4t4/net/cell_rate_4t4.sh"
OUT="$ROOT/analysis/formal4t4"

POLICY=${1:?usage: run_dynamic_4t4.sh <RateFIFO|LatestOnly|StaticSemantic|Adaptive> <tag> <seed> <manifest>}
TAG=${2:?missing tag}
SEED=${3:?missing seed}
MANIFEST=${4:?missing frozen manifest}

case "$POLICY" in
  RateFIFO|LatestOnly|StaticSemantic|Adaptive) ;;
  *) echo "unsupported dynamic policy: $POLICY" >&2; exit 2;;
esac
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }
BURST=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["ratefifo_burst_frames"])' "$MANIFEST")

LOG="$OUT/raw/dynamic/${TAG}.log"
EVENTS="$OUT/raw/dynamic/phases_${TAG}.jsonl"
mkdir -p "$OUT/raw/dynamic"
if [[ -e "$LOG" || -e "$EVENTS" ]]; then
  echo "refusing to overwrite dynamic evidence for tag=$TAG" >&2
  exit 3
fi

# Keep requests flowing through each of the three 45-second phases.
"$PY" "$HARNESS" \
  --stage dynamic --tag "$TAG" --seed "$SEED" --frozen-manifest "$MANIFEST" \
  --instances 4 --repetitions 1 --workload reuse_intensive \
  --n-requests 320 --warmup 24 --pool-size 64 --overlap 0.25 --alpha 1.2 --steps 3 \
  --concurrency 4 --output-tokens 4 --kv-cache-tokens 104544 \
  --rhos 0.5 --policies "$POLICY" --global-topk 16 --relay-max-inflight 4 --rate-burst-frames "$BURST" \
  --cooldown-s 0.5 >"$LOG" 2>&1 &
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
LOW_RATE=$(sed -nE 's/.*cell rate set: link=([0-9]+)bit.*/\1/p' "$LOG" | tail -1)
[[ -n "$LOW_RATE" ]] || { echo "could not parse initial low rate" >&2; abort; exit 4; }
HIGH_RATE=$((LOW_RATE * 5 / 12))  # rho 0.5 -> rho 1.2
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
sleep 45
stamp complete "$LOW_RATE"

wait "$HARNESS_PID"
trap - INT TERM
