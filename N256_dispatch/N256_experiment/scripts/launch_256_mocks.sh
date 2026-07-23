#!/bin/bash
# Launch 256 mock vLLM instances on ports 28000-28255.
# Usage: ./launch_256_mocks.sh [fail_rate]
#   fail_rate: 0-1, fraction of instances to mark as failing (default 0)
set -e

N=256
BASE_PORT=28000
LOG_DIR="/home/byh/B02/N256_dispatch/N256_experiment/logs"
mkdir -p "$LOG_DIR"
PID_DIR="/home/byh/B02/N256_dispatch/N256_experiment/pids"
mkdir -p "$PID_DIR"

FAIL_RATE=${1:-0}

# Kill any existing
pkill -9 -f "mock_vllm.py" 2>/dev/null || true
sleep 1

echo "[$(date +%T)] launching $N mock vllm instances (fail_rate=$FAIL_RATE)"

for i in $(seq 0 $((N-1))); do
    PORT=$((BASE_PORT + i))
    INST_ID="mock_${i}"
    PID_FILE="$PID_DIR/${INST_ID}.pid"
    LOG_FILE="$LOG_DIR/${INST_ID}.log"
    INSTANCE_FAIL_PROB=0
    # pick which instances to mark as failing: those at indices with bit 0 set in i
    if [ "$FAIL_RATE" != "0" ]; then
        # Simple deterministic: every 5th instance fails
        if [ $((i % 5)) -eq 0 ]; then
            INSTANCE_FAIL_PROB=0.5
        fi
    fi
    INSTANCE_FAIL_PROB=$INSTANCE_FAIL_PROB python3 /home/byh/B02/N256_dispatch/N256_experiment/scripts/mock_vllm.py \
        --port "$PORT" --instance-id "$INST_ID" --base-latency-ms 80 --jitter-ms 40 \
        > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
done

echo "[$(date +%T)] waiting for /health on all $N instances"
ok=0
for attempt in {1..120}; do
    sleep 2
    ok=0
    for i in $(seq 0 $((N-1))); do
        PORT=$((BASE_PORT + i))
        if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            ok=$((ok + 1))
        fi
    done
    if [ "$ok" -eq "$N" ]; then
        echo "[$(date +%T)] all $N healthy"
        break
    fi
    if [ $((attempt % 5)) -eq 0 ]; then
        echo "[$(date +%T)] attempt $attempt: $ok / $N healthy"
    fi
done

if [ "$ok" -ne "$N" ]; then
    echo "[$(date +%T)] only $ok / $N healthy after 4 min"
    exit 1
fi
echo "[$(date +%T)] all $N mock instances running, fail_rate=$FAIL_RATE"