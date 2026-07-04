#!/bin/bash
# Launch 4 vLLM instances (one per GPU) for B02 Motivation Experiment.
# Frozen params per experiments/design.md §2.1.
set -e

MODEL_PATH_FILE="$HOME/B02/poc/state_extraction/logs/model_path.txt"
if [ -f "$MODEL_PATH_FILE" ]; then
    MODEL="$(head -1 "$MODEL_PATH_FILE")"
else
    MODEL="Qwen/Qwen2.5-1.5B-Instruct"
    echo "[$(date +%T)] WARNING: no local model path file, will try HF id $MODEL"
fi

VENV="$HOME/B02/poc/.venv"
LOG_DIR="$HOME/B02/experiments/results"
mkdir -p "$LOG_DIR"

# shellcheck disable=SC1091
source "$VENV/bin/activate"

GPU_UTIL="${GPU_UTIL:-0.60}"
MAX_LEN="${MAX_LEN:-2048}"
MAX_SEQS="${MAX_SEQS:-64}"
SWAP="${SWAP:-4}"
BLOCK="${BLOCK:-16}"
START_GAP_S="${START_GAP_S:-20}"

echo "[$(date +%T)] model=$MODEL"
echo "[$(date +%T)] launching 4 vLLM instances SEQUENTIALLY on GPUs 0-3, ports 8000-8003"
echo "[$(date +%T)] gap between launches: ${START_GAP_S}s, gpu_mem_util=$GPU_UTIL"

for I in 0 1 2 3; do
    PORT=$((8000 + I))
    PID_FILE="$LOG_DIR/vllm_${I}.pid"
    LOG_FILE="$LOG_DIR/vllm_${I}.log"

    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[$(date +%T)] GPU$I PID=$(cat "$PID_FILE") already running, skip"
        continue
    fi

    echo "[$(date +%T)] starting GPU$I port=$PORT ..."
    CUDA_VISIBLE_DEVICES=$I \
    HF_ENDPOINT=https://hf-mirror.com \
    nohup vllm serve "$MODEL" \
        --port "$PORT" \
        --gpu-memory-utilization "$GPU_UTIL" \
        --max-model-len "$MAX_LEN" \
        --max-num-seqs "$MAX_SEQS" \
        --enable-prefix-caching \
        --swap-space "$SWAP" \
        --block-size "$BLOCK" \
        --enforce-eager \
        > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "[$(date +%T)]   PID=$(cat "$PID_FILE") log=$LOG_FILE"

    # Wait for /health on THIS instance before launching next
    echo "[$(date +%T)] waiting for GPU$I /health ..."
    READY=0
    for j in {1..120}; do
        sleep 5
        if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
            echo "[$(date +%T)] GPU$I READY (after $((j*5))s)"
            READY=1
            break
        fi
        if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "[FAIL] GPU$I died, last 15 log lines:"
            tail -15 "$LOG_FILE"
            exit 1
        fi
    done
    if [ "$READY" -ne 1 ]; then
        echo "[FAIL] GPU$I not ready within 10 min"
        tail -20 "$LOG_FILE"
        exit 1
    fi
    if [ "$I" -lt 3 ]; then
        echo "[$(date +%T)] sleeping ${START_GAP_S}s before next launch"
        sleep "$START_GAP_S"
    fi
done

echo "[$(date +%T)] ALL 4 instances READY"
for I in 0 1 2 3; do
    PORT=$((8000 + I))
    echo "  instance_$I -> http://127.0.0.1:$PORT"
    curl -fsS "http://localhost:$PORT/v1/models" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    model:", d["data"][0]["id"])'
done