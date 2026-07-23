#!/usr/bin/env bash
# S3 at concurrency 8: full fleet cleanup (incl. EngineCore zombies), restart,
# wave-probe (8 x 3), then the full paired run.
set -u
LOG=/home/byh/B02/supplemental_20260719/s3_c8_driver.log
exec >>"$LOG" 2>&1
set -x
date '+%F %T'
pkill -9 -f 'VLLM::EngineCor[e]' || true
pkill -9 -f '[v]llm serve' || true
sleep 8
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
bash /home/byh/B02/supplemental_20260715/restart_t4_vllm_long_context_v5.sh 9216 || exit 1
/home/byh/B02/poc/.venv/bin/python /home/byh/B02/supplemental_20260719/wave_probe.py 8 3
PROBE=$?
echo "wave_probe_exit=$PROBE"
if [ "$PROBE" -ne 0 ]; then
  echo "probe failed; aborting S3-c8"
  exit 1
fi
NPROC=$(pgrep -fc '[v]llm serve' || echo 0)
echo "vllm processes after probe: $NPROC"
if [ "$NPROC" -lt 4 ]; then
  echo "fleet degraded by probe; aborting S3-c8"
  exit 1
fi
cd /home/byh/B02/supplemental_20260715
/home/byh/B02/poc/.venv/bin/python run_live_k_tradeoff_v5.py \
  --out-dir /home/byh/B02/supplemental_20260719/live_concurrency8 \
  --zipf-alpha 0.55 --k-values 4,16 --repetitions 12 --n-requests 192 --warmup 64 \
  --active-prefixes 96 --concurrency 8 --prefix-tokens 2048 --output-tokens 4 --cooldown-s 0.05
echo "s3c8_exit=$?"
date '+%F %T'
