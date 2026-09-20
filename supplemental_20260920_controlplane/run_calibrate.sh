#!/usr/bin/env bash
# Step A: unconstrained 4-GPU event yield. No fan-in, μ=∞, no netem.
set -euo pipefail
ROOT=/home/byh/B02
OUT="$ROOT/supplemental_20260920_controlplane"
cd "$ROOT"
KV="${1:-104544}"
bash "$OUT/net/setup_net.sh" --rebuild --sig-bit 100000000 --netem-delay 0 --mtu 1500
source "$ROOT/poc/.venv/bin/activate"
exec python "$OUT/run_controlplane.py" \
  --stage calibrate --tag calibrate --out-dir "$OUT" \
  --workload reuse_intensive --repetitions 1 --seed 20260726 \
  --kv-cache-tokens "$KV" --drain-fps 0 --cluster-n 4 --relay-max-inflight 32
