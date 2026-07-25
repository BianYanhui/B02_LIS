#!/usr/bin/env bash
# Continue the frozen 4T4 study after the independently started baseline
# matrix.  This coordinator changes no experiment parameter: it only refuses
# to advance until both baseline summaries exist, then runs later stages in a
# single fail-fast sequence.
#
# Usage: run_remaining_formal4t4.sh <frozen_manifest.json>
set -euo pipefail

ROOT=/home/byh/B02
MANIFEST=${1:?usage: run_remaining_formal4t4.sh <frozen_manifest.json>}
OUT="$ROOT/analysis/formal4t4"
[[ -f "$MANIFEST" ]] || { echo "manifest missing: $MANIFEST" >&2; exit 2; }

ORIGINAL="$OUT/summary/cells_baseline_original_20260726.csv"
REUSE="$OUT/summary/cells_baseline_reuse_20260726.csv"
while [[ ! -f "$ORIGINAL" || ! -f "$REUSE" ]]; do
  if ! pgrep -f '[r]un_formal_baselines_4t4.sh' >/dev/null; then
    echo "baseline exited without both frozen summaries" >&2
    exit 3
  fi
  sleep 30
done

"$ROOT/experiments/4t4/run_dynamic_matrix_4t4.sh" "$MANIFEST"
"$ROOT/experiments/4t4/run_churn_matrix_4t4.sh" "$MANIFEST"
"$ROOT/experiments/4t4/run_background_matrix_4t4.sh" "$MANIFEST"
"$ROOT/experiments/4t4/run_native_validation_matrix_4t4.sh"
"$ROOT/poc/.venv/bin/python" "$ROOT/experiments/4t4/analyze_formal4t4.py" --root "$OUT"

# Vector figures are rendered and visually inspected after copying the final
# package to the local macOS workspace, where Poppler is available.
echo "formal4t4_remaining_stages_complete"
