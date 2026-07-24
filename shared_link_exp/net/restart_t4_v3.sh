#!/usr/bin/env bash
# Restart the T4 vLLM cluster for the v3 platform with a parameterized KV
# pressure tier.  Usage: restart_t4_v3.sh [LOW|MED|HIGH|<float>]
#   LOW=0.60 (less pressure), MED=0.40 (v1/v2 setting, ~104k tokens),
#   HIGH=0.30 (near the 8x4096-token scheduler floor).
# Prints KV_CACHE_TOKENS=<n> parsed from the server log at the end.
set -euo pipefail

TIER="${1:-MED}"
case "$TIER" in
  LOW)  MEM_UTIL=0.60;;
  MED)  MEM_UTIL=0.40;;
  HIGH) MEM_UTIL=0.30;;
  *)    MEM_UTIL="$TIER";;
esac

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
LOG_DIR="$ROOT/shared_link_exp/server_logs"

mkdir -p "$LOG_DIR"

# Killing only the B02 venv's vllm processes is authorized.
mapfile -t PIDS < <(pgrep -f "$ROOT/poc/.venv/bin/vllm serve" || true)
if ((${#PIDS[@]})); then
  kill "${PIDS[@]}" || true
  for _ in $(seq 1 30); do
    pgrep -f "$ROOT/poc/.venv/bin/vllm serve" >/dev/null || break
    sleep 1
  done
fi
if pgrep -f "$ROOT/poc/.venv/bin/vllm serve" >/dev/null; then
  pgrep -f "$ROOT/poc/.venv/bin/vllm serve" | xargs -r kill -9
fi

for GPU in 0 1 2 3; do
  PORT=$((8000 + GPU))
  CUDA_VISIBLE_DEVICES="$GPU" nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization "$MEM_UTIL" \
    --max-model-len 4096 --max-num-seqs 8 \
    --enable-prefix-caching --enable-prompt-tokens-details \
    --swap-space 4 --block-size 16 --enforce-eager \
    >"$LOG_DIR/vllm_${GPU}.log" 2>&1 &
  echo $! >"$LOG_DIR/vllm_${GPU}.pid"
done

for ATTEMPT in $(seq 1 180); do
  READY=0
  for PORT in 8000 8001 8002 8003; do
    if curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
      READY=$((READY + 1))
    fi
  done
  if ((READY == 4)); then
    sleep 2
    KV=$(sed -nE 's/.*GPU KV cache size: ([0-9,]+) tokens.*/\1/p' "$LOG_DIR/vllm_0.log" | tail -1 | tr -d ',')
    [ -n "$KV" ] || { echo "could not parse GPU KV cache size" >&2; exit 1; }
    echo "shared_link_vllm_ready mem_util=${MEM_UTIL}"
    echo "KV_CACHE_TOKENS=${KV}"
    exit 0
  fi
  sleep 2
done

echo "vllm readiness timeout" >&2
tail -n 80 "$LOG_DIR"/vllm_*.log || true
exit 1
