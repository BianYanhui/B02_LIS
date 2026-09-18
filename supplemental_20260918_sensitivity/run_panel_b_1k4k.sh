#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/byh/B02
KV=104544
cd "$ROOT"
for LEN in 1024 2048 4096; do
  echo "==== START L=$LEN $(date -u +%FT%T) ===="
  PYTHONUNBUFFERED=1 bash supplemental_20260918_sensitivity/run_panel_b.sh "$KV" "$LEN"
  echo "==== DONE L=$LEN $(date -u +%FT%T) ===="
done
