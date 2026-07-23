#!/usr/bin/env bash
# Item-1 driver: smoke both variants, then full normal + eviction runs.
set -u
LOG=/home/byh/B02/supplemental_20260720/crosssystem_driver.log
exec >>"$LOG" 2>&1
set -x
date '+%F %T'
PY=/home/byh/B02/poc/.venv/bin/python
S=/home/byh/B02/supplemental_20260720

bash "$S/restart_t4_vllm_util.sh" 9216 0.85 || exit 1
$PY "$S/run_live_crosssystem_v1.py" --smoke --repetitions 2 --n-requests 64 --warmup 32 \
  --out-dir "$S/smoke_crosssystem_normal" --variant normal || exit 1
$PY "$S/run_live_crosssystem_v1.py" \
  --out-dir "$S/crosssystem_normal" --variant normal \
  --active-prefixes 96 --cache-capacity 128 --sglang-assumed-capacity 512
echo "normal_exit=$?"

bash "$S/restart_t4_vllm_util.sh" 9216 0.50 || exit 1
$PY "$S/run_live_crosssystem_v1.py" --smoke --repetitions 2 --n-requests 64 --warmup 32 \
  --out-dir "$S/smoke_crosssystem_eviction" --variant eviction \
  --active-prefixes 384 --cache-capacity 64 --sglang-assumed-capacity 256 || exit 1
$PY "$S/run_live_crosssystem_v1.py" \
  --out-dir "$S/crosssystem_eviction" --variant eviction \
  --active-prefixes 384 --cache-capacity 64 --sglang-assumed-capacity 256
echo "eviction_exit=$?"
date '+%F %T'
