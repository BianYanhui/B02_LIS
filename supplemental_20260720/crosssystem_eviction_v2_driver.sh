#!/usr/bin/env bash
# Eviction variant v2: util 0.30 (physical KV ~24 prefixes/instance), model
# capacity 24, sglang assumed capacity 96, pool 384, 192 req/cell.
# Smoke gate: abort if sglang_approx stale_belief_rate does not materialize.
set -u
LOG=/home/byh/B02/supplemental_20260720/crosssystem_eviction_v2.log
exec >>"$LOG" 2>&1
set -x
date '+%F %T'
PY=/home/byh/B02/poc/.venv/bin/python
S=/home/byh/B02/supplemental_20260720

bash "$S/restart_t4_vllm_util.sh" 9216 0.30 || exit 1
rm -rf "$S/smoke_crosssystem_eviction_v2"
$PY "$S/run_live_crosssystem_v1.py" --smoke --repetitions 2 --n-requests 192 --warmup 64 \
  --out-dir "$S/smoke_crosssystem_eviction_v2" --variant eviction \
  --active-prefixes 384 --cache-capacity 24 --sglang-assumed-capacity 96 || exit 1
STALE=$($PY - <<'EOF'
import csv
rows = list(csv.DictReader(open('/home/byh/B02/supplemental_20260720/smoke_crosssystem_eviction_v2/crosssystem_cells.csv')))
sg = [float(r['stale_belief_rate']) for r in rows if r['policy'] == 'sglang_approx']
sk = [float(r['stale_belief_rate']) for r in rows if r['policy'] == 'sketch_coverage_k16']
print(max(sg), max(sk))
EOF
)
echo "smoke stale rates (sglang sketch): $STALE"
SG=$(echo $STALE | cut -d' ' -f1)
OK=$($PY -c "print(1 if float('$SG') > 0.005 else 0)")
if [ "$OK" != "1" ]; then
  echo "stale did not materialize (sglang=$SG); aborting before full run"
  exit 1
fi
$PY "$S/run_live_crosssystem_v1.py" \
  --out-dir "$S/crosssystem_eviction" --variant eviction \
  --active-prefixes 384 --cache-capacity 24 --sglang-assumed-capacity 96
echo "eviction_v2_exit=$?"
date '+%F %T'
