# SOTA Policy Matrix Conclusion

We added a SOTA-inspired policy matrix to test whether the Minimal State Sketch conclusion survives beyond the earlier weighted toy dispatcher.

## Policy Families

- `dualmap`: two stable prefix-hash candidates plus load-aware choice.
- `power2`: classic request-salted power-of-two load balancing.
- `slo_affinity`: affinity-first routing with an SLO/load fallback.

## State Interfaces

- `load_only`: no resident-prefix metadata.
- `exact_affinity`: full resident-prefix directory.
- `sketch_K`: bounded top-K resident-prefix directory per instance.

## Scenarios

- `stable`: ample KV capacity and high prefix locality.
- `eviction`: finite KV capacity and medium prefix locality.
- `remap_restart`: hash remapping and instance restart events.

## Main Result

The core paper claim remains valid under SOTA-inspired policies:

> Sketch K=8/K=16 often captures most of ExactAffinity's reuse benefit while using much less metadata.

Representative K=8 rows from `sota_policy_matrix_summary.csv`:

- `power2 / stable`: Sketch captures 94.18% of Exact reuse gain with 57.25% of Exact metadata.
- `power2 / remap_restart`: Sketch captures 94.19% of Exact reuse gain with 61.67% of Exact metadata.
- `slo_affinity / eviction`: Sketch captures 100% of Exact reuse gain with 45.48% of Exact metadata.
- `dualmap / remap_restart`: Sketch captures 88.05% of Exact reuse gain with 61.77% of Exact metadata.

## Important Caveat

DualMap-load is already a strong baseline in stable prefix-locality traces because stable prefix hashing co-locates repeated prefixes without any resident-state index. Therefore, a fair paper should not compare Sketch only against naive load balancing.

The better claim is:

> Minimal State Sketch is a bounded resident-state interface that can plug into multiple modern routing logics. It approximates ExactAffinity under DualMap-style, Power-of-Two, and SLO-aware routing while bounding metadata.

## How to Use in the Paper

Use `sota_policy_matrix_summary.csv` as the cross-policy generality table.

Recommended table columns:

- Policy family
- Scenario
- Load-only reuse
- Exact reuse
- Sketch K=8 reuse
- Sketch/Exact reuse-gain capture
- Exact metadata
- Sketch metadata
- Sketch/Exact metadata ratio

Do not use the CPU trace TTFT columns as primary evidence; they are model-derived and queue-light. Use live T4/A800 experiments for TTFT.

