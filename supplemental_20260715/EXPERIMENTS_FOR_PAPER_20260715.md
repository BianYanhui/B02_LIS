# B02 Experiments for Paper Writing

This directory contains supplemental data for **Cost-Aware State Interfaces for LLM Request Dispatch**.

Primary AI-facing workbook:

- `B02_AI_Experiment_Master_Table_20260715.xlsx`
- Start with sheets `Evidence_Scorecard`, `Claim_Map`, `Data_Dictionary`, and `Caveats`.

## New Supplemental Experiments Run on yhs1

1. `admission_ablation.csv`
   - CPU-only top-K interface simulation.
   - Compares `random_k`, `lru_k`, `coverage_k`, and `demand_aware`.
   - Supports the bounded-affinity-interface claim.
   - Important nuance: demand-aware is a concrete heuristic, not universally best.

2. `staleness_validation.csv`
   - Injects stale metadata: expired lease, wrong epoch, evicted resource, model mismatch.
   - Owner-side validation keeps `unsafe_reuse_rate = 0` up to 20% injected stale entries.
   - Supports the correctness/fallback claim.

3. `rate_controlled_event_driven.csv`
   - Token-bucket event-driven dissemination simulation.
   - Low-churn setting shows orders-of-magnitude lower traffic than periodic `N*K*f` reporting.
   - Use as control-plane model evidence, not a full live network implementation.

4. `live_vllm_affinity_workload.csv`
   - Short live T4/vLLM sanity check using the already-running Qwen2.5-1.5B endpoints on ports 8000-8007.
   - High-locality workload: Sketch/Exact routing obtains much higher reuse opportunity and lower TTFT than coarse.
   - Low-locality workload: benefit is small, supporting the workload-sensitivity limitation.

## Safe Paper Claims

- Exact/Rich reusable-state visibility is much more expensive than coarse load state.
- Load-only dispatch misses prefix-affinity opportunities when agentic workflows have prefix locality.
- Bounded Sketch/top-K state can recover useful affinity while bounding metadata size and fanout.
- Event-driven updates scale with resource changes and a rate budget, rather than periodic full-state reporting.
- Stale metadata affects placement quality but not execution correctness because owners validate KV before reuse.
- Benefits are workload-dependent; low-locality workloads and small-model TTFT should be presented as limitations.

## Claims to Avoid or Weaken

- Do not claim Rich always improves quality; Sketch often beats Rich in the existing data.
- Do not claim TTFT gains are universal; T4/Qwen2.5-1.5B results are noisy and mainly a sanity check.
- Do not use the old K-sweep `same_inst_step_ratio` as strong evidence; it is non-discriminative in that implementation.
- Do not present N=256 mock results as real 256-GPU serving.
- Do not present event-driven results as a deployed transport implementation.

