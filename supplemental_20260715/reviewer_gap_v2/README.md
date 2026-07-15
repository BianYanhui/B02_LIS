# B02 reviewer-gap v2 CPU evidence

All rows in this directory are either `simulation` or `microbenchmark`. They are valid for control-plane interface, bound, and correctness claims, but are not live serving measurements.

## Current primary files

- `cost_scaling.csv`: current `a=<d,m,nu,h,c,s,q,tau>` schema; replaces legacy Rich-size comparison.
- `supp_admission_v2.csv`: 540 online-admission cells, including 5 independent repetitions and demand shift tests.
- `j_bound_sweep.csv`: raw and evaluated fanout with enforced J.
- `staleness_validation_v2.csv` and `toctou_races.csv`: validation value and protocol race coverage.
- `budget_freshness_quality.csv`: four dissemination baselines coupled to modeled dispatch quality.
- `experiment_registry.csv`, `sanity_checks.csv`, and `trace_hash_manifest.json`: lineage and reproducibility evidence.

The T4 live replay output is intentionally written by a separate script so evidence types cannot be confused.
