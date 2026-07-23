#!/usr/bin/env bash
# Probe-and-run driver for S3: kill fleet, restart, wave-probe, then run S3 if probe passes.
set -u
LOG=/home/byh/B02/supplemental_20260719/s3_driver.log
exec >>"$LOG" 2>&1
set -x
date '+%F %T'
pgrep -f 'vllm serve' | xargs -r kill -9
sleep 3
bash /home/byh/B02/supplemental_20260715/restart_t4_vllm_long_context_v5.sh 9216 || exit 1
/home/byh/B02/poc/.venv/bin/python /home/byh/B02/supplemental_20260719/wave_probe.py 12 3
PROBE=$?
echo "wave_probe_exit=$PROBE"
if [ "$PROBE" -ne 0 ]; then
  echo "probe failed; aborting S3"
  exit 1
fi
NPROC=$(pgrep -fc 'vllm serve' || echo 0)
echo "vllm processes after probe: $NPROC"
if [ "$NPROC" -lt 4 ]; then
  echo "fleet degraded by probe; aborting S3"
  exit 1
fi
cd /home/byh/B02/supplemental_20260715
/home/byh/B02/poc/.venv/bin/python run_live_k_tradeoff_v5.py \
  --out-dir /home/byh/B02/supplemental_20260719/live_concurrency12 \
  --zipf-alpha 0.55 --k-values 4,16 --repetitions 12 --n-requests 192 --warmup 64 \
  --active-prefixes 96 --concurrency 12 --prefix-tokens 2048 --output-tokens 4 --cooldown-s 0.05
echo "s3_exit=$?"
date '+%F %T'
