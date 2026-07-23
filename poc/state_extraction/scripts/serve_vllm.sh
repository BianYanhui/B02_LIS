#!/bin/bash
# Launch vLLM OpenAI-compatible server in background on yhs1.
# Usage:
#   ./serve_vllm.sh [model_or_path] [port]
#
# model_or_path:
#   - a /local/path          -> used as --model (e.g. ModelScope cache)
#   - a HF repo id            -> used with $HF_ENDPOINT (default https://hf-mirror.com)
#   - empty / 'auto'          -> use $MODEL_PATH_FILE (logs/model_path.txt) if present,
#                                else fall back to default HF id
set -e

DEFAULT_HF_ID="Qwen/Qwen2.5-1.5B-Instruct"
MODEL_PATH_FILE="$HOME/B02/poc/state_extraction/logs/model_path.txt"

MODEL="${1:-auto}"
PORT="${2:-8000}"
GPU_UTIL="${GPU_UTIL:-0.85}"
MAX_LEN="${MAX_LEN:-4096}"
HF_EP="${HF_ENDPOINT:-https://hf-mirror.com}"

VENV="$HOME/B02/poc/.venv"
LOG_DIR="$HOME/B02/poc/state_extraction/logs"
PID_FILE="$LOG_DIR/vllm.pid"
LOG_FILE="$LOG_DIR/vllm_serve.log"

mkdir -p "$LOG_DIR"

# Resolve 'auto' to a real path/id
if [ "$MODEL" = "auto" ] || [ -z "$MODEL" ]; then
    if [ -f "$MODEL_PATH_FILE" ]; then
        MODEL="$(head -1 "$MODEL_PATH_FILE")"
        echo "[$(date +%T)] using local model path from $MODEL_PATH_FILE -> $MODEL"
    else
        MODEL="$DEFAULT_HF_ID"
        echo "[$(date +%T)] no local path file, falling back to HF id $MODEL"
    fi
fi

# Refuse if already running
if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
    echo "[FAIL] vLLM already running, PID=$(cat $PID_FILE)"
    echo "       run stop_vllm.sh first if you want to restart"
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ -n "$HF_EP" ] && [[ "$MODEL" != /* ]]; then
    export HF_ENDPOINT="$HF_EP"
    echo "[$(date +%T)] HF_ENDPOINT=$HF_ENDPOINT (only used for non-local model lookups)"
fi
echo "[$(date +%T)] launching: vllm serve $MODEL --port $PORT"
nohup vllm serve "$MODEL" \
    --port "$PORT" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --max-model-len "$MAX_LEN" \
    > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

PID=$(cat $PID_FILE)
echo "[$(date +%T)] PID=$PID log=$LOG_FILE"
echo "[$(date +%T)] waiting for /health ..."

for i in {1..180}; do
    sleep 5
    if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
        echo "[$(date +%T)] READY (after $(($i*5))s)"
        echo "[$(date +%T)] try: curl http://localhost:$PORT/v1/models"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[FAIL] vLLM process died, last 30 log lines:"
        tail -30 "$LOG_FILE"
        exit 1
    fi
done
echo "[FAIL] vLLM did not become ready within 15 min"
tail -30 "$LOG_FILE"
exit 1
