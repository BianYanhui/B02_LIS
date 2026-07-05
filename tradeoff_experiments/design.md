# B02 Trade-off Experiment — Design

**Date**: 2026-07-05
**Status**: Frozen before experiment run
**Companion**: original experiment in `~/B02/experiments/`

---

## 1. The Trade-off Being Verified

The user posed:
> "获取状态越多,Dispatch 质量理论上越好;但获取状态本身有开销,会导致 Dispatch 时间增大。"

The B02 Motivation Experiment (Ver.2) measured **state size and maintenance cost**, but
**did not isolate the quality vs cost trade-off**. It also could not observe any quality
difference between policies, because:
- 1.5B model on T4 has fast prefill (200-300 ms for 256 tokens) — cache miss is small
- Workload was light: ~10 RPS per instance, instances never saturated
- Prompts were short and varied — no shared prefix to cache

This experiment **engineers a workload that exposes the trade-off**:

```
                  ┌──────────────────────────────────────────┐
                  │  State size (bytes) / update frequency   │
                  │       = cost per second per instance     │
                  │                                          │
                  │       ┌─ Coarse: ~360 B                  │
                  │       ├─ Rich: ~2.4 KB (7× larger)        │
                  │       └─ Sketch: ~350 B (≈ Coarse)        │
                  │                                          │
   Dispatch  ────►│  Higher f → fresher state → better       │
   quality        │  quality BUT higher dispatcher CPU       │
   (cache hit)    │                                          │
                  │  More state per update → better          │
                  │  quality (Rich uses affinity) BUT more   │
                  │  bytes to ship per second                │
                  └──────────────────────────────────────────┘
```

**Hypothesis to verify**:
1. **Quality axis**: `cache_hit_rate(Rich) > cache_hit_rate(Coarse) > cache_hit_rate(Round-Robin)`
2. **Cost axis**: `dispatch_decision_time(Rich) ≈ dispatch_decision_time(Coarse)` but
   `state_traffic(Rich) >> state_traffic(Coarse)`
3. **Trade-off**: at high update frequency, the cost of Rich may dominate; at low
   frequency, the quality gain of Rich may be invisible because state is stale.

---

## 2. Workload Design — engineered to expose affinity signal

To make the trade-off observable, we need a workload with:
- **Long shared prefix** that vLLM can cache and re-use
- **Multi-step workflow** so the dispatcher can route multiple steps of the same workflow
- **Enough concurrency** that some instances get loaded

### 2.1 Affinity-tuned agentic workload

Each workflow has 16 steps. Each step sends the same long system prompt (a fixed 500-token
preamble that vLLM can prefix-cache) + a per-step user message:

```
system prompt (500 tokens, FIXED — designed for prefix caching):
"You are a helpful AI assistant. Below is a conversation between a user and an AI.
The AI is helpful, accurate, and concise. Answer the user's question step by step,
reasoning carefully. Provide your answer in numbered points when appropriate.
[repeated to ~500 tokens]"

user message per step: "Step N/16: <task-specific question>"
```

This makes:
- The system prompt identical across all steps and all workflows → strong prefix-cache benefit
- Each step's user message is unique → KV cache for the prefix is shared
- A dispatcher that routes a workflow to the same instance across steps benefits from
  cached prefix → faster prefill

### 2.2 Workload parameters (frozen)

| Parameter | Value | Why |
|---|---|---|
| System prompt length | ~500 tokens (filled by repeating template) | long enough that cache hit matters |
| Steps per workflow | **16** | enough that the workflow has multiple routing decisions |
| Concurrent workflows | **12** (3 per instance avg) | enough load to make routing decisions matter, not enough to OOM |
| Per-step output | 32 tokens | short, fast generation |
| Workflows per cell | 10 | total 160 step-dispatches per cell |
| Measurement duration | 60 s after warmup | enough samples per policy |
| Warmup | 20 s (dispatcher + 2 warmup workflows) | prime prefix cache |

### 2.3 Cell grid (smaller than original Part A because each cell is richer)

| Axis | Levels | Count |
|---|---|---|
| Policy | round-robin, coarse, rich, sketch | 4 |
| Update frequency | 1, 10, 50 Hz | 3 |
| Reps | 2 | 2 |
| **Total** | | **24 cells** |

Per cell: ~120 s (warmup 20 + measurement 60 + agentic workflow 30 + overhead)
**Total budget: 24 × 120s = 48 min** (well within remaining time)

---

## 3. Metrics

### 3.1 Quality metrics (per cell)

| Metric | Source | Interpretation |
|---|---|---|
| **prefix_cache_hit_rate** | `vllm:gpu_prefix_cache_hits_total / vllm:gpu_prefix_cache_queries_total` | **Primary quality metric.** Higher = dispatcher is making better routing decisions. |
| `ttft_p50, p95, p99` (ms) | from request_log | TTFT in streaming mode (we'll add streaming=True this time) |
| `request_latency_p50, p95, p99` (ms) | from request_log | e2e latency |
| `workflow_completion_p50, p95, p99` (ms) | from workflow log | all 16 steps done |
| `step_count_success_rate` | workflow log | fraction of workflows completing all 16 steps |

### 3.2 Cost metrics (per cell)

| Metric | Source | Interpretation |
|---|---|---|
| **state_traffic_B_per_sec** | n_instances × avg_size × update_freq | total bytes/sec shipped from instances to dispatcher |
| `dispatch_decision_us_p50, p95, p99` | dispatcher forward() | how long the policy decision itself takes |
| `dispatcher_cpu_pct` (estimated) | from serial 4-instance scrape time | rough CPU share consumed by state collection |
| `state_size_p95_bytes` | state_updates.jsonl | how big each individual state update is |
| `stale_state_ratio` | (target_f - actual_f) / target_f | how often the dispatcher couldn't keep up |

### 3.3 The trade-off curve

The headline plot is **Quality vs Cost** with one dot per (policy, frequency) configuration:

- **X axis**: state_traffic_B_per_sec (log scale)
- **Y axis**: prefix_cache_hit_rate
- **Series**: one curve per policy (RR/Coarse/Rich/Sketch), 3 dots each (one per freq)

Expected pattern:
- RR: low quality regardless of traffic (no state used)
- Coarse: moderate quality, modest traffic growth with f
- Rich: high quality at high f (fresh state, full affinity), but traffic 7× higher
- Sketch: ~Rich quality at ~Coarse traffic (the win)

---

## 4. Policy changes (vs `experiments/scripts/dispatcher.py`)

The dispatcher from the original experiment already has 4 policies. We **add one new
policy: "rich-affinity"** that aggressively routes workflows to their last-assigned instance
to maximize cache hit rate. This is the strongest possible "use rich state" baseline.

```python
def policy_rich_affinity(state_view, request, ctx) -> str:
    """Always route to the workflow's last instance if it's not too loaded.
    This represents the strongest possible use of rich affinity state."""
    wf_id = request.get("workflow_id")
    if wf_id and wf_id in ctx["workflow_table"]:
        wf = ctx["workflow_table"][wf_id]
        if wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            if recent in ctx["instances"]:
                # check this instance isn't overloaded (>80% of waiting+running cap)
                v = state_view.get(recent, {})
                rt = v.get("runtime", v)
                if "num_requests_running" in rt:
                    if rt["num_requests_running"] < 50:
                        return recent
    # fall back to coarse load balancing
    return policy_coarse(state_view, request, ctx)
```

---

## 5. Frozen inputs (identical to original experiment)

- 4×T4, vLLM 0.10.2, Qwen2.5-1.5B-Instruct
- orjson serialization
- 30s warmup, 60s measurement
- All cells use the same workload (long-prefix agentic)

---

## 6. Output layout

```
tradeoff_experiments/
├── design.md                            # this file
├── scripts/
│   ├── dispatcher_tradeoff.py           # extends dispatcher with rich-affinity policy
│   ├── workload_affinity.py             # long-prefix agentic workload
│   ├── run_tradeoff_cell.py             # one cell
│   ├── run_tradeoff.py                  # orchestrator
│   └── aggregate_tradeoff.py            # tables + figures + report
├── results/
│   ├── cells/<cell_id>/
│   │   ├── state_updates.jsonl
│   │   ├── dispatch_log.jsonl
│   │   ├── workflow.jsonl
│   │   ├── request_log.jsonl
│   │   ├── metrics_summary.json         # per-cell quality + cost summary
│   │   └── cache_metrics.jsonl          # cache hit rate samples
│   ├── aggregates/
│   │   ├── tradeoff_quality.csv
│   │   ├── tradeoff_cost.csv
│   │   └── tradeoff_curve.csv
│   ├── figures/
│   │   ├── fig1_quality_vs_traffic.png  # the trade-off plot
│   │   ├── fig2_cache_hit_per_policy.png
│   │   └── fig3_dispatch_latency_per_freq.png
│   └── report.md                        # final analysis
```

---

## 7. Threats to Validity (extends prior)

- **1.5B model is small**: 7B+ models would show larger absolute cache-miss cost
- **Loopback network**: real network may add 1-10 ms to state collection, shifting the trade-off
- **Synthetic workload**: real agent traces may have less consistent prefix structure
- **Single-host**: cross-host state collection would test the cost side more rigorously
- **Synthetic 500-token "system prompt"**: real system prompts may be 2-10k tokens
- **No streaming mode** (we'll add it for this experiment): non-streaming TTFT = e2e latency
