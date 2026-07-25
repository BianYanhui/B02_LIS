#!/usr/bin/env bash
# Execute the requested 3-policy × 3-background-level matrix with paired seeds.
set -euo pipefail
ROOT=/home/byh/B02
MANIFEST=${1:?usage: run_background_matrix_4t4.sh <frozen_manifest.json>}
for POLICY in FullSync RateFIFO Adaptive; do
  for LEVEL in none medium high; do
    "$ROOT/experiments/4t4/run_background_4t4.sh" "$POLICY" "$LEVEL" \
      "background_${POLICY}_${LEVEL}_20260726" 2026072800 "$MANIFEST"
  done
done
