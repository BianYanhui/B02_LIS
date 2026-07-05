#!/bin/bash
# Orchestrator: run remaining tiers after Tier 1 finishes.
# This script is invoked once Tier 1 is done.
set -e

source ~/B02/poc/.venv/bin/activate
cd ~/B02/full_experiment/scripts

echo "[$(date +%T)] === starting tier 2 quality (prefix_locality) ==="
python run_full.py --tier quality --reps 2 --duration-s 75 \
  > ~/B02/full_experiment/results/run_t2.log 2>&1
echo "[$(date +%T)] tier 2 done"

echo "[$(date +%T)] === starting tier 3 tool-delay ==="
python run_full.py --tier sensitivity --reps 2 \
  > ~/B02/full_experiment/results/run_t3.log 2>&1
echo "[$(date +%T)] tier 3 done"

echo "[$(date +%T)] === starting tier 4 mixed ==="
python run_full.py --tier mixed --reps 2 \
  > ~/B02/full_experiment/results/run_t4.log 2>&1
echo "[$(date +%T)] tier 4 done"

echo "[$(date +%T)] === starting tier 5 bursty ==="
python run_full.py --tier bursty --reps 2 \
  > ~/B02/full_experiment/results/run_t5.log 2>&1
echo "[$(date +%T)] tier 5 done"

echo "[$(date +%T)] === starting stress test ==="
python run_stress.py --N 4 64 256 --freq 10 50 --views coarse rich sketch --reps 2 --duration-s 30 \
  > ~/B02/full_experiment/results/run_stress.log 2>&1
echo "[$(date +%T)] stress done"

echo "[$(date +%T)] === aggregating ==="
python aggregate.py \
  > ~/B02/full_experiment/results/aggregate.log 2>&1
echo "[$(date +%T)] all done"
