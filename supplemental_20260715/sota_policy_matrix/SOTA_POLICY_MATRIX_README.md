# SOTA Policy Matrix for B02

This experiment evaluates Minimal State Sketch under three SOTA-inspired dispatch policy families: DualMap-style, Power-of-Two, and SLO-aware affinity fallback.

The experiment isolates the state-interface variable: `load_only`, `exact_affinity`, and `sketch_K`.

Primary files:

- `sota_policy_matrix.csv`: full per-cell results.
- `sota_policy_matrix_summary.csv`: claim-facing comparison of Sketch vs Exact under each policy/scenario.

Use this to support the paper claim that Minimal State Sketch is an interface contribution, not a policy-specific weighted-score heuristic.
