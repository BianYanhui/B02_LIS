#!/usr/bin/env bash
# Live grid: N={4,16,32,64} x RateFIFO/StaticSemantic/Adaptive x 5 reps.
set -euo pipefail
ROOT=/home/byh/B02
OUT="$ROOT/supplemental_20260920_controlplane"
cd "$ROOT"
KV="${1:-104544}"
bash "$OUT/net/setup_net.sh" --rebuild --sig-bit 100000000 --netem-delay 40 --netem-jitter 5 --mtu 1500
source "$ROOT/poc/.venv/bin/activate"
exec python "$OUT/run_controlplane.py" \
  --stage fanin --tag fanin --out-dir "$OUT" \
  --workload reuse_intensive --repetitions 5 --seed 20260726 \
  --policies RateFIFO,StaticSemantic,Adaptive \
  --cluster-ns 4,16,32,64 --drain-fps 200 --sync-s 1.0 \
  --kv-cache-tokens "$KV" --relay-max-inflight 32 --rate-burst-frames 20
