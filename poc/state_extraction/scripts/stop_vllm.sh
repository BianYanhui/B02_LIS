#!/bin/bash
# Stop a vLLM server launched via serve_vllm.sh
set -e

LOG_DIR="$HOME/B02/poc/state_extraction/logs"
PID_FILE="$LOG_DIR/vllm.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "[INFO] no PID file at $PID_FILE; nothing to stop"
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "[$(date +%T)] stopping vLLM PID=$PID ..."
    kill -TERM "$PID" 2>/dev/null || true
    for i in {1..30}; do
        sleep 2
        kill -0 "$PID" 2>/dev/null || break
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "[$(date +%T)] still alive, sending KILL"
        kill -KILL "$PID" || true
    fi
else
    echo "[INFO] PID $PID not running"
fi
rm -f "$PID_FILE"
echo "[$(date +%T)] done"
