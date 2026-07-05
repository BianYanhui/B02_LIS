#!/bin/bash
# Launch 8 vLLM instances (2 per GPU) for v3 trade-off experiment.
# Sequential launch, gpu_mem=0.40 to fit 2 instances on 15GB T4.
set -e

MODEL_PATH_FILE="$HOME/B02/poc/state_extraction/logs/model_path.txt"
MODEL="$(head -1 "$MODEL_PATH_FILE")"

VENV="$HOME/B02/poc/.venv"
LOG_DIR="$HOME/B02/tradeoff_experiments/results_v3"
mkdir -p "$LOG_DIR"

source "$VENV/bin/activate"

GPU_UTIL="${GPU_UTIL:-0.40}"
MAX_LEN="${MAX_LEN:-2048}"
MAX_SEQS="${MAX_SEQS:-24}"
SWAP="${SWAP:-2}"
BLOCK="${BLOCK:-16}"
GAP_S="${GAP_S:-15}"

echo "[$(date +%T)] launching 8 vLLM instances (2 per GPU, sequential)"
echo "[$(date +%T)] model=$MODEL gpu_mem=$GPU_UTIL max_seqs=$MAX_SEQS"

# 8 instances: instance 0..7 on ports 8000..8007, GPU mapping floor(i/2)
INST_IDX=0
for GPU in 0 1 2 3; do
    for SLOT in 0 1; do
        PORT=$((8000 + GPU * 2 + SLOT))
        PID_FILE="$LOG_DIR/vllm_${INST_IDX}.pid"
        LOG_FILE="$LOG_DIR/vllm_${INST_IDX}.log"
        echo "[$(date +%T)] starting instance_$INST_IDX GPU=$GPU port=$PORT ..."
        CUDA_VISIBLE_DEVICES=$GPU \
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
        echo "  PID=$(cat $PID_FILE) log=$LOG_FILE"
        # Wait for /health on THIS instance
        READY=0
        for j in {1..120}; do
            sleep 5
            if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
                echo "[$(date +%T)] instance_$INST_IDX READY (after $((j*5))s)"
                READY=1
                break
            fi
            if ! kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
                echo "[FAIL] instance_$INST_IDX died, last 15 log lines:"
                tail -15 "$LOG_FILE"
                exit 1
            fi
        done
        if [ "$READY" -ne 1 ]; then
            echo "[FAIL] instance_$INST_IDX not ready within 10 min"
            tail -20 "$LOG_FILE"
            exit 1
        fi
        if [ "$INST_IDX" -lt 7 ]; then
            sleep "$GAP_S"
        fi
        INST_IDX=$((INST_IDX + 1))
    done
done

echo "[$(date +%T)] ALL 8 instances READY"
for I in 0 1 2 3 4 5 6 7; do
    PORT=$((8000 + I))
    echo "  instance_$I -> http://127.0.0.1:$PORT"
    curl -fsS "http://localhost:$PORT/v1/models" 2>/dev/null | python3 -c 'import json,sys; print("    model:", json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || echo "    (no model info)"
done