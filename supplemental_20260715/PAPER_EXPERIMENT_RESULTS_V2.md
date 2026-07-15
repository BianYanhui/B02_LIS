# B02 Paper Experiment Writing Notes (v2)

Use only `Current` rows in `experiment_registry_v2.csv` as primary evidence. Do not cite sources listed as `Legacy_DoNotCite` or `Sanity_only` in `legacy_data_status.csv`.

## Recommended Evaluation Structure

1. **Current-schema interface cost.** Report `cost_scaling.csv` at N=128, R=256, K=8. It isolates Load-only, Exact Affinity, and Sketch under the current 64-byte affinity-entry schema.
2. **Same-trace state-interface quality.** Use frozen rows for the information-bound result and closed-loop rows for sampled live T4 TTFT/reuse validation. State exactly that observed reuse is dispatcher-observed, because vLLM does not export cache-hit counters.
3. **Cross-policy generality.** Use the existing `sota_policy_matrix` CPU traces for DualMap-style, Power-of-Two, and SLO-aware families. Keep their modeled TTFT separate from live TTFT.
4. **Bounds and correctness.** Use `j_bound_sweep.csv`, `budget_freshness_quality.csv`, `staleness_validation_v2.csv`, and `toctou_races.csv` as explicit control-plane simulation evidence.

## Claim Decisions

### C1: Supported

Bounded Sketch reduces the current Exact Affinity interface cost.

Evidence: N=128,R=256,K=8: snapshot Sketch=77824B vs Exact=2109440B; update Sketch=6553.6B/s vs Exact=209715.2B/s.

Writing rule: Use as the replacement for legacy Rich-size Table 1.

### C2: Supported with workload scope

Exact visibility is an information upper bound and bounded K can approach it under locality.

Evidence: Frozen high locality: Exact reuse=0.9077333333333333; Sketch-K8=0.8424666666666667. Closed-loop high: Exact=0.5571428571428572; Sketch-K16=0.5571428571428572.

Writing rule: State frozen results as controlled interface evidence; label closed-loop observed reuse as dispatcher-observed.

### C3: Supported

Validation keeps stale metadata from authorizing unsafe reuse.

Evidence: Validation-on max unsafe reuse=0.0; seven TOCTOU race maximum unsafe count=0.

Writing rule: Correctness evidence only, not a latency speedup.

### C4: Supported as simulation

Budgeted event-driven dissemination can retain quality under low/mid churn and exposes no-rate overload.

Evidence: B4 low churn saved=180000.0 vs periodic=90000.0; B4 mid stale=0.0; B3 high-churn p95 delay=1550.0ms.

Writing rule: Call this a control-plane simulation, not a live metadata transport benchmark.

### C5: Negative result / Route B

Current demand-aware utility should not be presented as the default superior admission policy.

Evidence: Full utility mean saved/exact: high K8=0.731, medium K16=0.678; high-to-low convergence mean=1500 requests.

Writing rule: Describe demand-aware admission as pluggable. Use LRU/coverage as default bounded admission in this workload generator.

## T4 Scope

Four Tesla T4 GPUs run one Qwen2.5-1.5B vLLM instance per GPU. The measured trace replay uses 64 requests per cell, 5 independent repetitions, approximate 256/512-token prompts, and four-request arrival waves. This is a sampled end-to-end validation, not a 1,050,000-request throughput study. A larger GPU (for example A800/80G) is needed for long-context or high-concurrency throughput claims.

## Admission Negative Result

The current decentralized demand-aware utility did not meet its preregistered saved-vs-Exact or shift-convergence targets. Revise the paper so Sketch is a bounded state interface with pluggable admission; use LRU/coverage admission as the default bounded baseline and report demand-aware as an ablation/limitation rather than a superior optimizer.
