# DualMap Adaptation Conclusion

We implemented a DualMap-style router for the B02 Minimal State Sketch system and ran CPU trace plus small live T4/vLLM sanity experiments.

## Implemented Policies

- `least_load`: load-only baseline with round-robin tie-breaking.
- `dualmap_load`: two stable prefix-hash candidates, choose the less-loaded candidate.
- `exact_affinity_dualmap`: DualMap candidates plus exact visible resident-prefix candidates.
- `sketch_affinity_dualmap_K`: DualMap candidates plus bounded Sketch-visible prefix candidates.

This avoids the earlier toy weighted score as the primary policy.

## Key Finding

DualMap itself is a strong cache-affinity baseline. In a stable repeated-prefix trace, `dualmap_load` already achieves high prefix reuse because the two hash mappings keep the same prefix near the same candidates.

This means the old experiment design would overstate Sketch's benefit if it only compares against a naive load-only dispatcher.

## Does DualMap Break the Paper Conclusion?

No, but it changes the right claim.

The correct claim becomes:

> Minimal State Sketch lets a DualMap-style cache-aware router use bounded, resident-state affinity metadata. It approaches ExactAffinity-DualMap while advertising far fewer entries.

The claim should not be:

> Sketch beats a hand-weighted load dispatcher.

## CPU Trace Results

In `dualmap_results_v2/dualmap_key_findings.csv`:

- High locality: Sketch K=8 reuse is essentially equal to Exact, with lower metadata.
- Medium locality: Sketch K=8 remains essentially equal to Exact, with lower metadata.
- Low locality: all cache-affinity methods converge, which supports workload sensitivity.

Important: `dualmap_load` is already very strong in this synthetic stable-prefix trace. Future experiments should include dynamic eviction, instance restart, scaling, or resident-state decoupling to show when exact/sketch resident metadata matters beyond pure hash affinity.

## Live T4/vLLM Sanity

In `dualmap_live_v2/dualmap_live_vllm.csv`, Sketch/Exact-DualMap are competitive with or better than DualMap-load on this small run, but the sample is too small for final claims.

Use it as a sanity check only.

## Recommended Paper Revision

Use DualMap as the main policy layer:

1. `LeastLoad`
2. `DualMap-Load`
3. `ExactAffinity-DualMap`
4. `SketchAffinity-DualMap(K)`

Then frame B02 as a state-interface paper:

- ExactAffinity-DualMap is the high-visibility upper bound.
- SketchAffinity-DualMap is the bounded-interface version.
- DualMap-Load is the SOTA-style no-resident-index baseline.

## Next Necessary Experiment

To make the Sketch advantage clearer under DualMap, add one dynamic-residency experiment:

- finite KV capacity with evictions,
- instance restart / epoch invalidation,
- scaling or hash-ring remap,
- or load/SLO fallback that sends some prefixes outside their original DualMap candidates.

These are exactly the cases where request-hash affinity alone is insufficient and resident-state metadata matters.

