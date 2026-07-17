# Paper-Ready Evidence and Wording (V6)

## Recommended Scope

Use the narrow claim: **B02 exposes a bounded resident prefix-KV affinity interface with explicit directory, traffic, and evaluated-fanout bounds; its quality depends on locality and admission, and stale advertisements remain hints rather than reuse capabilities.**

Do not claim universal latency improvement, globally optimal budget allocation, arbitrary reusable-state semantics, universal policy support, or a direct performance win over SGLang/Preble.

## Runtime-Native Correctness Paragraph

We integrated the owner operation with vLLM 0.10.2's live `BlockPool`: `ValidateAndPin` checks the advertised digest, tenant, model revision, epoch, sequence, lease, and current cache coverage, then invokes `BlockPool.touch()` in the EngineCore utility loop; release invokes `BlockPool.free_blocks()` on the same runtime. On one Tesla T4 with a 2337-token prompt (2336 full cached tokens), 1024 valid validations from 16 concurrent HTTP clients all pinned and released successfully (owner-operation p50/p95/p99 13.7/20.8/26.6 ms; 566 operations/s). Across 448 injected stale cases covering epoch, sequence, tenant, model revision, lease expiry, physical eviction, and restart epoch, unsafe reuse was zero. During 128 concurrent eviction attempts, every eviction was rejected while the prefix was pinned; after the cancellation-release branch, the prefix became evictable.

Writing constraint: call these **single-host runtime-owner operations over loopback HTTP**. The measurement includes the developer API and utility-channel overhead, not only a critical-section CPU cost. The cancellation test exercises the release branch; it is not yet a transparent hook on every OpenAI client disconnect path.

## Cross-Policy Paragraph

For the candidate-based adapter scope, paired trace-derived replay shows that Sketch-K16 retains 0.950 of Exact's incremental saved-prefill under P2C and 0.926 under DualMap-style routing, each with a 4480-byte dispatcher index in the reported setup. For a deadline-abstaining SLO-aware candidate policy, steady-state Sketch-K32 retains 0.803 of Exact. These are replay/model outcomes, not live end-to-end SLO measurements.

## Closest-System Positioning

Preble is the closest integrated distributed prompt scheduler; SGLang Router is the closest current router-side cache-aware system. The paper should compare their state boundaries rather than assert a direct unrun latency comparison. B02's defensible delta is independent contracts for per-instance advertisement cardinality K, metadata dissemination B, evaluated fanout J, and owner-validated stale hints. Use `related_system_interface_matrix_v6.csv` as the source for a compact related-system table.

## Mandatory Limitations

- The native integration is a B02 experimental vLLM 0.10.2 developer API on one host, not a production upstream API or multi-node deployment.
- The cross-policy adapter targets per-request candidate-based policies; batch-global schedulers and joint routing/eviction optimizers remain out of scope.
- SGLang Router and Preble are not directly benchmarked here because they are unavailable on yhs1 and do not share B02's physical-KV state semantics; a direct latency bar would confound whole-stack differences.
- Existing live latency evidence remains scoped as in V5; do not use it to claim a universal TTFT reduction.
