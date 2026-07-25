#!/usr/bin/env bash
# Run the pre-registered low -> high -> low trace five times for each
# comparison policy.  Each wrapper invocation owns one trace and never
# overwrites evidence from an earlier repetition.
#
# Usage: run_dynamic_matrix_4t4.sh <frozen_manifest.json>
set -euo pipefail

ROOT=/home/byh/B02
RUN_ONE="$ROOT/experiments/4t4/run_dynamic_4t4.sh"
MANIFEST=${1:?usage: run_dynamic_matrix_4t4.sh <frozen_manifest.json>}
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }

for POLICY in RateFIFO LatestOnly StaticSemantic Adaptive; do
  for REP in 0 1 2 3 4; do
    TAG="dynamic_${POLICY}_rep${REP}_20260726"
    SEED=$((2026072700 + REP))
    "$RUN_ONE" "$POLICY" "$TAG" "$SEED" "$MANIFEST"
  done
done
