#!/usr/bin/env bash
# Smoke 2: N=32, μ=200/s, netem 40ms. Expect L>μ and Adaptive gate to arm.
set -euo pipefail
ROOT=/home/byh/B02
OUT="$ROOT/supplemental_20260920_controlplane"
cd "$ROOT"
KV="${1:-104544}"
bash "$OUT/net/setup_net.sh" --rebuild --sig-bit 100000000 --netem-delay 40 --netem-jitter 5 --mtu 1500
source "$ROOT/poc/.venv/bin/activate"
exec python "$OUT/run_controlplane.py" \
  --stage smoke_n32 --tag smoke_n32 --out-dir "$OUT" \
  --workload reuse_intensive --repetitions 1 --seed 20260726 \
  --kv-cache-tokens "$KV" --drain-fps 200 --cluster-n 32 --relay-max-inflight 32
