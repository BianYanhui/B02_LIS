# B02 Environment Boundaries (V4)

## Completed On Current T4/CPU

- Current-schema interface-cost scaling, bounded J sweep, stale-state/TOCTOU protocol simulations, and control-plane sensitivity.
- Paired AgentTrace-derived admission/K diagnosis with an offline Oracle-K upper bound.
- Paired P2C and DualMap-style cross-policy replay using the same coverage-first Sketch and net-benefit guard.
- Fixed-prompt T4/vLLM sampled live replay with vLLM cached-token telemetry, once the active primary run completes its data-quality checks.

These results support an explicitly scoped state-cost versus reusable-prefill-quality interface claim. They do not establish a production throughput claim.

## Worth Renting One A800/80G For

An A800 is useful for the following experiments that are memory-constrained or too noisy on four 15GB T4s:

1. **Long-context live validation**: Qwen2.5-7B/14B, 8K/16K shared prefixes, 16/32-token fixed outputs, and concurrency 1/4/8/16. Record input/output tokens, cached tokens, paired TTFT, and prefill-token counters.
2. **Larger-model robustness**: Repeat the fixed-prompt K16/K32 versus Exact comparison at 7B or 14B, where saved prefill time amortizes dispatcher overhead more clearly.
3. **Multi-replica single-host serving**: Host two or more carefully isolated replicas where memory permits, then replay a controlled queue/affinity conflict with real vLLM telemetry.
4. **Prefill calibration**: Measure miss versus cached-prefix prefill time at 512/2K/4K/8K/16K to calibrate the net-benefit guard instead of using a simulator-only tokens-to-ms rate.

Twelve hours is enough for a focused 7B long-context paired matrix, not for a credible 128-GPU scale-out claim. Start with a 30-minute endpoint and telemetry smoke test before committing the rest of the rental.

## Not Solved By Either Single-Machine Environment

1. **Real distributed metadata transport**: validating token-bucket/coalescing over independent dispatcher and owner nodes needs multiple hosts, real network queues, and transport instrumentation.
2. **Large-N serving scale**: N=128 GPU directory behavior requires a cluster or a carefully stated simulation; one 80GB card cannot establish this result.
3. **Agent semantic/task validity**: structural AgentTrace replay cannot prove coding-agent success, tool correctness, or production workflow arrival realism. This needs a public agent benchmark and actual task execution.
4. **End-to-end owner-side concurrent KV safety**: a proof under live concurrent eviction/restart requires a vLLM/serving integration that exposes ValidateAndPin and owner epoch/lease guards, not only dispatcher simulation.

## Paper Scope Consequence

The present paper should frame agentic systems as a motivating class of prefix-reusing dependent workloads. It should not claim broad production agent coverage, distributed transport deployment, or universal latency/throughput gains until the above environments are available.
