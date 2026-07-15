# Reviewer Feedback Actions and Resource Boundaries (v3)

## Completed on yhs1 (4x Tesla T4)

1. **Order-balanced live serving replay.** `paired_t4_latency_v3` uses one
   logical trace per locality/repetition, randomizes policy order, uses a
   separate vLLM prefix-cache namespace for every policy cell, and reports
   paired deltas, median, IQR, bootstrap CI, and scatter-ready cells. It is
   the only V3 source permitted to make sampled live TTFT statements.
2. **Nontrivial J-bound analysis.** `control_plane_v3` introduces candidate
   heterogeneity (reuse coverage, expected saved prefill, queue delay, and
   expiry risk), so J=1 no longer trivially equals an unbounded candidate set.
   This is explicitly a dispatcher-level simulation.
3. **Repeated budget/freshness sensitivity.** The same control-plane suite
   uses 20 independent event streams for every budget/churn/baseline cell and
   reports bootstrap CIs. A finite per-instance link capacity is applied to
   every baseline; `event_driven_no_rate` means no sender pacing/coalescing,
   not infinite transport.
4. **Trace-derived agent workload evidence.** `agenttrace_structural_v3`
   derives anonymized structural features from the public Apache-2.0 AgentTrace
   NL2Bash tool-execution split: prompt-length proxy, turn order, common
   template hash, prior tool-span timing, and SHA-256 lineage. Raw prompts,
   reasoning, tool inputs and tool outputs are excluded from all result files.
   The output is a trace-derived dispatcher simulation, not a semantic agent
   benchmark or live T4 latency result.

## Worth Running on One A800/80G

A single 80 GB A800 is useful for **model-scale and context-scale calibration**:

1. Measure real vLLM prefix-cache hit/miss prefill savings for Qwen2.5-7B or
   14B at 4K/8K/16K input lengths, after adding an explicit vLLM metric or a
   request-level profiler. This calibrates the paper's `saved_prefill` proxy.
2. Run high-repetition single-server TTFT/throughput sweeps at longer contexts
   and higher concurrency than a 15 GB T4 permits. Report the result as
   single-server prefix-cache behavior, not as a dispatch result.
3. Run owner-side validation microbenchmarks with large KV residency and long
   prefixes to quantify compatibility-check and fallback overhead at scale.

One A800 does **not** replace four independent serving instances. Running four
vLLM processes on one physical GPU shares the compute scheduler and KV memory;
it cannot support a clean multi-instance routing/queueing conclusion. For a
stronger end-to-end dispatch result, rent at least four isolated GPU instances
or use four separate GPUs with per-instance endpoints.

## Not Solved by Either Current Environment Alone

1. **Representative production-agent workload evidence.** Public traces are
   useful structural evidence, but claims about broad agentic serving require
   a released benchmark trace or privacy-reviewed production trace with
   arrival times, full prefix lineage, tool latency, model revision, and
   tenancy information.
2. **Real metadata transport deployment.** A token-bucket simulation does not
   substitute for multi-host serialization, network queueing, packet loss,
   and clock/failure behavior. This requires a distributed control-plane
   implementation and at least several network-isolated serving nodes.
3. **Actual cross-instance KV reuse correctness.** Stock independent vLLM
   servers do not transfer KV blocks across owners. Proving the full
   `ValidateAndPin` contract therefore requires an owner-side KV interface or
   a vLLM patch, concurrent fault injection, and observability of real cache
   pin/evict events.
4. **128/256-GPU scaling.** Neither four T4s nor one A800 can substantiate a
   physical 128/256-GPU serving claim. Keep N=128 cost scaling as an analytic
   current-schema interface microbenchmark, not a deployment-scale result.

## Paper Wording Rule

Keep live T4, trace-derived replay, and control-plane simulation in separate
figures/tables. The defensible headline is a bounded state-interface
cost-quality trade-off for prefix-reusing workloads, with explicit locality
and evidence-type scope.
