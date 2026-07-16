# B02 Paper Experiment Writing Notes (V5)

## Evidence Discipline

Use `paper_claim_evidence_v5.csv`, `experiment_registry_v5.csv`, and `sanity_checks_v5.csv` before drafting. Do not pool live T4, trace-derived simulation, dispatcher simulation, single-host microbenchmark, and cost microbenchmark metrics into one serving-performance claim.

## Current Thesis

A bounded affinity interface creates an explicit state-cost versus reusable-prefill-quality trade-off. Its value depends on locality concentration, admission quality, candidate bound, and whether reusable coverage repays queue and validation overhead.

## Legacy Exclusions

V3 paired T4 rows remain `Superseded_InputConfounded`. The first SLO V5 run is `Invalid_NativeVisibilityLeak`: its native baseline accessed private coverage and is retained only for audit, not as evidence.

## Claim Decisions

### C7: Supported as paired SLO-aware dispatcher replay

The bounded interface can be attached to an SLO-aware affinity fallback policy while preserving deadline abstention.

Evidence: Steady SLO replay: K32 retains 0.803 of Exact saved-prefill at 8576B versus Exact 33152B; SLO miss rate is 0.012 versus Load-only 0.588. Burst K32 retainment is 0.911.

Writing rule: Call TTFT and deadline results modeled dispatcher outcomes; do not present them as live serving SLO evidence.

### C8: Supported as single-host implementation microbenchmark

Reference owner validation and fixed-width metadata transport have bounded local serialization/IPC cost.

Evidence: 32 concurrent loopback clients: valid ValidateAndPin p95=2121.4us, throughput=18120 ops/s; all incompatible scenarios fallback with unsafe reuse count 0. All 8 token-bucket cells obeyed the one-frame burst budget assertion (failures=0).

Writing rule: State single-host loopback TCP and reference owner implementation; it is neither inter-node transport nor a vLLM-native KV pin result.

### C9: Supported as fixed-prompt live T4 K sweep

A difficult active-prefix workload exposes a live bounded-state K trade-off rather than a single Sketch=Exact point.

Evidence: K4/K16/K32 Exact-normalized estimated saved-prefill means are 0.528/0.948/1.000; corresponding mean index bytes are 1408/4475/5333.

Writing rule: Report paired TTFT distributions and physical cached-token telemetry separately from estimated saved-prefill; do not claim a generic throughput/SLO result.

## Required Limitations

- AgentTrace-derived replay preserves structural turn patterns, not semantic task completion or production arrivals.
- SLO TTFT/deadline outcomes are modeled dispatcher quantities.
- TCP measurements are single-host loopback and use a reference owner; vLLM-native KV pin/eviction is not implemented.
- Larger-model and long-context live validation remain a separate hardware/replica-scaling question.