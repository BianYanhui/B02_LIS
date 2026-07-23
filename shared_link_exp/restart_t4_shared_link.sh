#!/usr/bin/env bash
# Rebuild the T4 serving cluster for the B02 shared-link hybrid experiment.
# Adapted from supplemental_20260715/restart_t4_vllm_trace_replay.sh:
# one vLLM server per GPU on ports 8000-8003 with prefix caching and
# per-response cached-token telemetry.  Killing only the B02 venv's vllm
# processes first is authorized.
set -euo pipefail

ROOT=/home/byh/B02
VLLM="$ROOT/poc/.venv/bin/vllm"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
LOG_DIR="$ROOT/shared_link_exp/server_logs"

mkdir -p "$LOG_DIR"

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
  # gpu-memory-utilization 0.40 (not the V4 default 0.85): at 0.85 this GQA
  # model gets a 349,984-token KV pool (~170 full 2048-token prefixes), so a
  # 128-request rep never evicts.  0.40 lands the pool near the paper's
  # intended regime (~60k tokens, ~29 prefixes/instance); the REAL capacity
  # is always read from these logs and passed to the harness.
  CUDA_VISIBLE_DEVICES="$GPU" nohup "$VLLM" serve "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization 0.40 \
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
    echo "shared_link_vllm_ready"
    exit 0
  fi
  sleep 2
done

echo "vllm readiness timeout" >&2
tail -n 80 "$LOG_DIR"/vllm_*.log || true
exit 1
