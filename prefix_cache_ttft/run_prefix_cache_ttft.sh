#!/usr/bin/env bash
# Dedicated one-instance launcher: does not modify the formal vLLM cluster.
set -euo pipefail
ROOT=/home/byh/B02
GPU="${GPU:-0}"
PORT="${PORT:-8010}"
OUT="$ROOT/analysis/prefix_cache_ttft"
MODEL=/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct
VLLM="$ROOT/poc/.venv/bin/vllm"
PY="$ROOT/poc/.venv/bin/python"
mkdir -p "$OUT"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is occupied; refusing to alter it" >&2; exit 1
fi
LOG="$OUT/vllm_gpu${GPU}_port${PORT}.log"
CUDA_VISIBLE_DEVICES="$GPU" nohup "$VLLM" serve "$MODEL" --host 127.0.0.1 --port "$PORT" \
  --gpu-memory-utilization 0.70 --max-model-len 12288 --max-num-seqs 1 \
  --enable-prefix-caching --enable-prompt-tokens-details --swap-space 4 --block-size 16 --enforce-eager >"$LOG" 2>&1 &
PID=$!; echo "$PID" > "$OUT/vllm_gpu${GPU}_port${PORT}.pid"
for _ in $(seq 1 180); do
  curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1 && break
  if ! kill -0 "$PID" 2>/dev/null; then tail -n 100 "$LOG" >&2 || true; exit 1; fi
  sleep 2
done
curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null || { tail -n 100 "$LOG" >&2; exit 1; }
"$PY" "$ROOT/prefix_cache_ttft/run_prefix_cache_ttft.py" --url "http://127.0.0.1:${PORT}" --output "$OUT" "$@"
