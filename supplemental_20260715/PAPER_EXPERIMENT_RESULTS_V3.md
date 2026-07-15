# B02 Paper Experiment Writing Notes (v3)

Use rows with `status=Current` in `experiment_registry_v3.csv`. Evidence types are not interchangeable: `live_t4_vllm` is sampled live serving, `trace_replay_simulation` and `trace_derived_simulation` are controlled state-interface evidence, and `control_plane_simulation` is not a live network benchmark.

## Evidence hierarchy

1. Interface cost: current-schema cost scaling is the primary quantitative cost claim.
2. Quality: frozen replay establishes the Exact information upper bound; large frozen traces establish locality scope.
3. Live T4: use only paired distributions from `paired_t4_latency_v3`, not an unpaired p95 mean dominated by a single repetition.
4. Agent workload: `agenttrace_structural_v3` derives lengths, turn structure and hashed lineage from public tool-execution traces; it is not a semantic task or live serving benchmark.
5. Control plane: heterogenous J and repeated budget sensitivity are explicitly simulations.

## Mandatory scope language

The paper should claim a bounded state-interface cost-quality trade-off for prefix-reusing workloads. It should not claim universal agentic workload coverage, a general throughput improvement, or a universally superior demand-aware admission policy.

## Claim decisions

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

### C6: Supported as heterogeneous control-plane simulation

J is a real resource-quality knob when affinity candidates differ in coverage, queue delay, and expiry risk.

Evidence: Across heterogeneous candidate cells, maximum mean J=4 quality loss is 0.0019; all evaluated p95 fanouts obey J.

Writing rule: Present a J Pareto curve and label it a dispatcher-level simulation; do not claim a production-optimal J.

### C7: Supported with trace-derived structural scope

The interface trade-off also appears under recorded tool-using agent turn structure.

Evidence: Public AgentTrace closed-loop structural replay: Exact saved=170339.6 prefix tokens; Sketch-K8=99796.8.

Writing rule: Call this trace-derived structural replay. It contains no raw text and does not establish semantic task accuracy or live latency.

### C8: Sampled live evidence only

Order-balanced T4 replay characterizes the paired latency distribution without an outlier-dominated aggregate.

Evidence: Sketch-K8 vs load-only paired p95 delta median=3.71ms, 95% bootstrap CI for mean=[-0.89, 4.77]ms; negative-delta fraction=0.35.

Writing rule: Report paired median/IQR/scatter and CI. Claim a TTFT improvement only when the paired distribution, not an unpaired mean, supports it.

### C9: Supported as repeated simulation

Control-plane freshness conclusions are reported with independent repetitions and confidence intervals.

Evidence: Repeated finite-link sensitivity includes 20 repetitions/configuration; event-driven no-rate high-churn p95 delay reaches up to 1998.0ms.

Writing rule: Retain the control-plane simulation label and avoid describing it as a measured metadata transport deployment.
