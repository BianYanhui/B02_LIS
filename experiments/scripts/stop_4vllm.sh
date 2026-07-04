#!/bin/bash
# Stop all 4 vLLM instances cleanly.
set -e

LOG_DIR="$HOME/B02/experiments/results"

for I in 0 1 2 3; do
    PID_FILE="$LOG_DIR/vllm_${I}.pid"
    if [ -f "$PID_FILE" ]; then
        PID="$(cat "$PID_FILE")"
        if kill -0 "$PID" 2>/dev/null; then
            echo "[$(date +%T)] stopping GPU$I PID=$PID"
            kill "$PID" || true
        else
            echo "[$(date +%T)] GPU$I PID=$PID not running"
        fi
        rm -f "$PID_FILE"
    else
        echo "[$(date +%T)] GPU$I no PID file"
    fi
done

sleep 5
for I in 0 1 2 3; do
    # Make sure no zombie vllm processes pinned to this GPU
    pkill -f "CUDA_VISIBLE_DEVICES=$I.*vllm" 2>/dev/null || true
done
sleep 3

echo "[$(date +%T)] GPU mem after stop:"
nvidia-smi --query-gpu=index,memory.used --format=csv