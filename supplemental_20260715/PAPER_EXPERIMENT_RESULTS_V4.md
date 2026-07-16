# B02 Paper Experiment Writing Notes (V4)

## Read First

Use `paper_claim_evidence_v4.csv`, `experiment_registry_v4.csv`, and `sanity_checks_v4.csv` before drafting. Evidence types are not interchangeable. `live_t4_vllm` is sampled live serving; `trace_derived_simulation` is a paired state-interface replay; `dispatcher_level_simulation` validates a decision mechanism; and cost microbenchmarks quantify interface overhead only.

## Current Thesis

A bounded affinity interface exposes a measurable metadata-cost versus reusable-prefill-quality trade-off. Its effectiveness depends on locality concentration, admission quality, and whether reusable coverage repays load/validation overhead. This work does not establish universal agentic workload coverage or general throughput/SLO dominance.

## Superseded Evidence

Do not cite `paired_t4_latency_v3`: policy namespace was embedded in semantic prompt and altered model output. Do not use `agenttrace_structural_v3` as a positive Sketch result: it is retained only as a negative admission diagnostic. V4 lineage is explicit in the master table.

## Claim Decisions

### C1: Supported

Bounded K explicitly reduces Exact-Affinity interface state cost.

Evidence: Use reviewer_gap_v2/cost_scaling.csv as the primary current-schema cost table; do not describe it as end-to-end serving resource saving.

Writing rule: Report snapshot/index/update cost separately from serving latency.

### C2: Supported with AgentTrace-derived workload scope

Coverage-first bounded admission can preserve most of guarded Exact saved-prefill value on recorded tool-using turn structure.

Evidence: Closed-loop AgentTrace: K16 retains 0.891 of Exact at 2560B vs Exact 16896B; K32 retains 0.981. Offline Oracle-K16 reaches 0.993.

Writing rule: Call Oracle an offline diagnostic only; describe K16 residual gap as admission headroom, not a deployable oracle result.

### C3: Supported as paired cross-policy replay

The bounded affinity interface adapts to both load-oriented P2C and locality-biased DualMap-style policies, with policy-dependent marginal benefit.

Evidence: P2C K16 retains 0.950 of Exact and is 2.753x Load-only; DualMap K16 retains 0.926 and is 1.453x DualMap-Load.

Writing rule: State interface compatibility and saved-prefill quality only; do not present these rows as live SLO/throughput evidence.

### C4: Supported as heterogeneous dispatcher simulation

Candidate fanout J is a real resource-quality control, not merely a formal bound.

Evidence: In heterogeneous J replay, maximum mean quality loss at J=4 is 0.0019, while evaluated fanout obeys J.

Writing rule: Do not call J=4 production-optimal; it is scenario-specific.

### C5: Supported as net-benefit sensitivity simulation

A coverage-and-queue abstention guard prevents short/queued affinity from degrading placement.

Evidence: At 256 reusable tokens and +50ms affinity queue, affinity-first p95=151.52ms; guarded p95=116.51ms with selection rate 0.000. At 4K reusable tokens and +50ms queue, guarded p95=55.25ms with selection rate 0.840.

Writing rule: Label as dispatcher-level sensitivity; it validates the decision rule, not a measured production queue.

### C6: Supported as sampled fixed-prompt live T4 evidence

Under a high-locality 2K-prefix workload, bounded coverage-first routing exposes physical vLLM cached tokens and can be evaluated with input-identical paired TTFT.

Evidence: K16 paired p50 TTFT delta vs Load-only mean=-326.52ms, 95% CI=[-471.11,-180.04]ms; Exact delta=-367.23ms. Inspect paired distribution before claiming a p95 improvement.

Writing rule: Use fixed prompt/output and vLLM cached-token telemetry. Describe this as sampled T4 validation, not throughput characterization.

## Required Limitations

- AgentTrace is structural/trace-derived; it does not establish semantic task completion or production arrival realism.
- Oracle-K is an upper-bound diagnosis only and must never be called deployable.
- Control-plane and guard studies are simulations, not a real distributed metadata transport benchmark.
- P2C/DualMap results demonstrate cross-policy interface adaptation, not universal policy gains.