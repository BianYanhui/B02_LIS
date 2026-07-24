#!/usr/bin/env bash
# End-to-end smoke for the v3 real-kernel signaling platform:
#   1. vLLM cluster up at MED KV pressure (restart if unhealthy)
#   2. b02-net docker platform up (network, image, gateway+bgserver, tc)
#   3. harness --smoke (1 rep x 48 requests; ideal + {exact_fifo, agg_full} x
#      rho {0.5, 1.3} + background-sharing A/B) with built-in assertions
# The platform is left RUNNING afterwards (that is the deliverable).
set -euo pipefail

ROOT=/home/byh/B02
HERE="$ROOT/shared_link_exp/net"
PY="$ROOT/poc/.venv/bin/python"
# Cache namespaces must be unique per invocation. Reusing a fixed tag makes a
# later smoke hit the previous run's real vLLM KV entries and invalidates the
# offered-rate calibration.
SMOKE_TAG="${SMOKE_TAG:-v3smoke-$(date +%Y%m%d%H%M%S)}"

parse_kv_capacity() {
  sed -nE 's/.*GPU KV cache size: ([0-9,]+) tokens.*/\1/p' \
    "$ROOT/shared_link_exp/server_logs/vllm_0.log" | tail -1 | tr -d ','
}

# 1. vLLM at MED pressure
READY=0
for PORT in 8000 8001 8002 8003; do
  curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1 && READY=$((READY + 1))
done
if ((READY == 4)); then
  KV=$(parse_kv_capacity)
  echo "vllm already healthy (KV_CACHE_TOKENS=$KV)"
else
  "$HERE/restart_t4_v3.sh" MED
  KV=$(parse_kv_capacity)
fi
[ -n "$KV" ] || { echo "could not parse GPU KV cache size" >&2; exit 1; }

# 2. docker + tc platform
"$HERE/setup_net.sh"

# 3. harness smoke (assertions inside; exits nonzero on failure)
mkdir -p "$ROOT/shared_link_exp/live_v3"
cd "$ROOT/shared_link_exp"
"$PY" run_live_shared_link_v3.py --smoke --tag "$SMOKE_TAG" --kv-cache-tokens "$KV" \
  --out-dir "$ROOT/shared_link_exp/live_v3" 2>&1 | tee "$ROOT/shared_link_exp/live_v3/smoke.log"
