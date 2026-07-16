#!/usr/bin/env bash
# Start four cache-isolated T4 workers with a configurable long-context limit.
set -euo pipefail

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
MAX_MODEL_LEN="${1:-9216}"
LOG_DIR="$ROOT/supplemental_20260715/live_k_tradeoff_v5_server_logs"

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

for GPU in 0 1 2 3; do
  PORT=$((8000 + GPU))
  CUDA_VISIBLE_DEVICES="$GPU" nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization 0.85 \
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
    echo "long_context_t4_ready max_model_len=$MAX_MODEL_LEN"
    exit 0
  fi
  sleep 2
done

echo "long-context T4 vLLM readiness timeout" >&2
tail -n 80 "$LOG_DIR"/*.log || true
exit 1
