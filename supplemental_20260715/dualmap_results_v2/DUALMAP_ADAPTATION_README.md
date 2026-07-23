# DualMap Adaptation Experiment

This experiment replaces the earlier hand-weighted dispatcher with a DualMap-style policy.

Policy definitions:

- `least_load`: load-only baseline.
- `dualmap_load`: two stable prefix hash candidates, choose less loaded candidate.
- `exact_affinity_dualmap`: DualMap candidates plus exact visible resident-prefix candidates.
- `sketch_affinity_dualmap_K`: DualMap candidates plus bounded Sketch-visible prefix candidates.

The important paper question is whether Sketch remains close to Exact while using fewer advertised entries. If yes, replacing the toy weighted policy does not break the Minimal State Sketch conclusion.
