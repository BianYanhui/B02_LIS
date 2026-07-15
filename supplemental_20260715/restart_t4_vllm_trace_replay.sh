#!/usr/bin/env bash
# Rebuild the T4 serving cluster for B02 same-trace replay.
set -euo pipefail

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
LOG_DIR="$ROOT/supplemental_20260715/trace_replay_v2/server_logs"

mkdir -p "$LOG_DIR"

# The user explicitly authorized replacing the eight previous vLLM servers.
mapfile -t PIDS < <(pgrep -f "$ROOT/poc/.venv/bin/vllm serve" || true)
if ((${#PIDS[@]})); then
  kill "${PIDS[@]}" || true
  for _ in $(seq 1 30); do
    if ! pgrep -f "$ROOT/poc/.venv/bin/vllm serve" >/dev/null; then
      break
    fi
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
    --gpu-memory-utilization 0.85 \
    --max-model-len 4096 --max-num-seqs 8 \
    --enable-prefix-caching --swap-space 4 --block-size 16 --enforce-eager \
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
    echo "trace_replay_vllm_ready"
    exit 0
  fi
  sleep 2
done

echo "vllm readiness timeout" >&2
tail -n 80 "$LOG_DIR"/vllm_*.log || true
exit 1
