#!/usr/bin/env bash
# Start four cache-isolated T4 workers with configurable gpu-memory-utilization.
# Zombie-safe: also kills VLLM::EngineCore processes whose cmdline was rewritten
# (the stock restart script's pgrep pattern misses them after a crash).
set -euo pipefail

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
MAX_MODEL_LEN="${1:-9216}"
GPU_UTIL="${2:-0.85}"
LOG_DIR="$ROOT/supplemental_20260720/server_logs"

mkdir -p "$LOG_DIR"
pkill -9 -f 'VLLM::EngineCor[e]' || true
pkill -9 -f '[v]llm serve' || true
for _ in $(seq 1 30); do
  pgrep -f 'VLLM::EngineCor[e]' >/dev/null || pgrep -f '[v]llm serve' >/dev/null || break
  sleep 1
done
sleep 3
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader

for GPU in 0 1 2 3; do
  PORT=$((8000 + GPU))
  CUDA_VISIBLE_DEVICES="$GPU" nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" --max-num-seqs 8 \
    --enable-prefix-caching --enable-prompt-tokens-details \
    --swap-space 4 --block-size 16 --enforce-eager \
    >"$LOG_DIR/vllm_${GPU}.log" 2>&1 < /dev/null &
  echo $! >"$LOG_DIR/vllm_${GPU}.pid"
done

for _ in $(seq 1 180); do
  ready=0
  for PORT in 8000 8001 8002 8003; do
    curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1 && ready=$((ready + 1))
  done
  if ((ready == 4)); then
    grep -h -o 'GPU KV cache size: [0-9,]* tokens' "$LOG_DIR"/vllm_*.log | sort -u
    echo "t4_util_ready max_model_len=$MAX_MODEL_LEN gpu_util=$GPU_UTIL"
    exit 0
  fi
  sleep 2
done

echo "T4 vLLM readiness timeout (util=$GPU_UTIL)" >&2
tail -n 60 "$LOG_DIR"/*.log || true
exit 1
