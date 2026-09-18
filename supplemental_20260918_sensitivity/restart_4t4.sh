#!/usr/bin/env bash
# Start the isolated 4xT4 formal cluster: one vLLM process per GPU/port.
# It never terminates an unrelated B02 server: only PIDs recorded in this
# experiment's own server-log directory may be replaced.  Usage:
#   restart_4t4.sh [gpu_memory_utilization] [max_model_len]
#
# The installed vLLM carries the B02 owner-validation developer endpoints.
# Enabling them exposes test-only HTTP routes but does not alter generation or
# scheduler settings; this lets the separate high-churn safety experiment use
# a real owner-side ValidateAndPin operation.
set -euo pipefail

MEM_UTIL="${1:-0.40}"
MAX_MODEL_LEN="${2:-6144}"
DEV_MODE="${VLLM_SERVER_DEV_MODE:-1}"

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
LOG_DIR="$ROOT/analysis/formal4t4/server_logs"

mkdir -p "$LOG_DIR"

# Replace only servers started by an earlier 4T4 run, and refuse to steal an
# occupied port from any unrelated process.
for pid_file in "$LOG_DIR"/vllm_*.pid; do
  [ -e "$pid_file" ] || continue
  PID="$(cat "$pid_file")"
  if ps -p "$PID" -o args= 2>/dev/null | grep -q "$ROOT/poc/.venv/bin/vllm serve"; then
    kill "$PID" || true
  fi
done
for _ in $(seq 1 30); do
  LIVE=0
  for pid_file in "$LOG_DIR"/vllm_*.pid; do
    [ -e "$pid_file" ] || continue
    kill -0 "$(cat "$pid_file")" 2>/dev/null && LIVE=1
  done
  (( LIVE == 0 )) && break
  sleep 1
done
for PORT in 8000 8001 8002 8003; do
  if ss -ltn "( sport = :$PORT )" | grep -q ":$PORT"; then
    echo "refusing to use occupied port $PORT" >&2
    exit 2
  fi
done

for GPU in 0 1 2 3; do
  PORT=$((8000 + GPU))
  CUDA_VISIBLE_DEVICES="$GPU" VLLM_SERVER_DEV_MODE="$DEV_MODE" nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization "$MEM_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" --max-num-seqs 8 \
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
    if [[ "$DEV_MODE" == "1" ]]; then
      for PORT in 8000 8001 8002 8003; do
        curl -fsS "http://127.0.0.1:${PORT}/b02/native_pin/status" >/dev/null || {
          echo "native validation endpoint unavailable on port ${PORT}" >&2
          exit 1
        }
      done
    fi
    echo "formal4t4_vllm_ready mem_util=${MEM_UTIL} max_model_len=${MAX_MODEL_LEN} native_validation=${DEV_MODE}"
    echo "KV_CACHE_TOKENS=${KV}"
    exit 0
  fi
  sleep 2
done

echo "vllm readiness timeout" >&2
tail -n 80 "$LOG_DIR"/vllm_*.log || true
exit 1
