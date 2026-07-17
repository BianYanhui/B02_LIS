#!/usr/bin/env bash
# Start the four existing T4 endpoints with B02's vLLM-native owner API enabled.
set -euo pipefail

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL="/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
LOG_DIR="$ROOT/supplemental_20260717/vllm_logs_v6"
mkdir -p "$LOG_DIR"

mapfile -t PIDS < <(pgrep -f "$ROOT/poc/.venv/bin/vllm serve" || true)
if ((${#PIDS[@]})); then
  kill "${PIDS[@]}" || true
  for _ in $(seq 1 30); do
    pgrep -f "$ROOT/poc/.venv/bin/vllm serve" >/dev/null || break
    sleep 1
  done
fi
pgrep -f "$ROOT/poc/.venv/bin/vllm serve" | xargs -r kill -9 || true

for gpu in 0 1 2 3; do
  port=$((8000 + gpu))
  log="$LOG_DIR/vllm_gpu${gpu}.log"
  CUDA_VISIBLE_DEVICES="$gpu" VLLM_SERVER_DEV_MODE=1 nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$port" \
    --gpu-memory-utilization 0.85 --max-model-len 4096 --max-num-seqs 8 \
    --enable-prefix-caching --enable-prompt-tokens-details \
    --swap-space 4 --block-size 16 --enforce-eager \
    >"$log" 2>&1 &
  echo "$!" > "$LOG_DIR/vllm_gpu${gpu}.pid"
done

echo "Started T4 vLLM endpoints on ports 8000-8003. Logs: $LOG_DIR"
