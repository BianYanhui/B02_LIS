# B02 Motivation Experiment — Design Document

**Date**: 2026-07-04
**Status**: Frozen before experiment run
**Companion doc**: `docx/prompt/B02_Motivation_Prompt.Ver.2.md`

---

## 1. Architecture Decisions (resolves Ver.2 gaps)

### 1.1 Process topology — **Option C**

- **4 vLLM servers**, one per GPU, each exposes `/v1/chat/completions` and `/metrics`
- **1 dispatcher** runs as a separate process on the same host
  - Owns the workflow state table (Ver.2 §11)
  - Polls each vLLM `/metrics` at the configured frequency
  - Receives per-workflow updates from the workflow client
  - Builds Coarse / Rich / Sketch state views
  - Runs the configured dispatch policy and forwards to one of the 4 instances

Rationale: cleanest separation, matches Ver.2 §11 description ("Dispatcher maintains workflow state table"). Avoids forking vLLM. Network is loopback — declared in Threats to Validity.

### 1.2 Dispatch policy scoring formulas (resolves Ver.2 G7)

For each request R, the dispatcher picks instance I* minimizing `score(I, R)`:

| Policy | `score(I, R)` formula |
|---|---|
| **Round-robin** | `-last_assigned_at[I]` (round-robin by last assignment timestamp) |
| **Coarse-Dispatch** | `α * (num_requests_waiting[I] + num_requests_running[I]) + β * kv_cache_usage_perc[I]` |
| **Rich-Dispatch** | `Coarse score + γ * affinity_score(I, R)` <br> where `affinity_score` = −(# of R's recent steps assigned to I) — i.e., reward locality |
| **Sketch-Dispatch** | `Coarse score + γ * quantized_affinity_score(I, R)` <br> where quantized affinity uses `affinity_hot_instance_counts[I]` discretized into low/mid/high |

Defaults: α=1, β=0.1, γ=10. Frozen before any pilot.

### 1.3 Latency summary source (resolves Ver.2 F3)

`latency_summary` (TTFT, TPOT, queue_time, prefill_time, decode_time p50/p95) is **computed from `request_log.jsonl`**, not from vLLM Prometheus histograms. The dispatcher records `(request_id, vllm_request_start_ns, first_token_ns, finish_ns)` per request, then derives `TTFT = first_token - start`, `TPOT = (finish - first_token) / output_tokens`, etc., and aggregates per cell. This avoids needing a t-digest and is honest about what "summary" means.

### 1.4 Sketch computation (resolves Ver.2 F5)

For a sketch built over `K` workflows assigned to instance I:

```
active_workflow_count     = K
avg_progress_q            = uint8(mean(progress_i) * 100, 0..100)
max_progress_q            = uint8(max(progress_i) * 100, 0..100)
tool_status_bitset        = pack tool_state_i as 2-bit field per workflow (K<=16)
                            bit layout: 00=idle, 01=running, 10=done, 11=failed
tool_context_avail_bitmap = 1 bit per workflow: 1=context available, 0=not
affinity_hot_counts       = uint16[N_instances]: count of workflows whose most-recent
                            step was on instance j, for j=0..N-1
recent_workflow_hashes    = uint32[4]: rolling 4-step hash of (workflow_id, step_id, tool_name)
```

All numbers are derived from `workflow_table` directly. Frozen.

### 1.5 Scale reductions vs. Ver.2 (6h budget)

| Ver.2 spec | Reduced | Reason |
|---|---|---|
| 5000–10000 reqs/cell for p99 chatbot | **1500 reqs/cell** | 6h budget, T4 throughput limit |
| 200–500 workflows/cell for agentic | **40 workflows/cell** | same |
| 5 reps Part A | **2 reps (3rd only on mismatch)** | per user instruction |
| 10 reps Part B | **2 reps** | per user instruction |
| 5 frequencies | **3 frequencies: 1, 10, 50 Hz** | main effect axis |
| Agentic tool delays {0, 100, 500} ms × 3 step counts | **3 step counts {4, 8, 16} × tool delay {200 ms fixed}** | full factorial too expensive |
| 60–120s per cell | **30s warmup + 60s measurement = 90s/cell** | fit in 6h |
| 3.3h Part A budget | **~2h Part A** | reduced cells |
| 4h Part B budget | **~30 min Part B** | logical emulator mode |

### 1.6 Cell count math

Part A:
```
2 workloads × 4 views × 3 freqs × 2 reps = 48 cells
48 cells × 90s = 4320s = 72 min
+ 4-instance startup (one-time): 60s × 4 = 4 min
= 76 min ≈ 1.3 h
```

Part B:
```
5 N values {4, 16, 64, 128, 256} × 3 freqs × 3 views × 2 reps = 90 cells
90 cells × 30s = 2700s = 45 min
```

Total budget: ~2.3h experiment + ~1h aggregation/figures/report = ~3.3h. **Well within 6h.**

---

## 2. Frozen Inputs

### 2.1 vLLM launch parameters (frozen per Ver.2 §4)

```bash
CUDA_VISIBLE_DEVICES=$I vllm serve $MODEL \
    --port $PORT \
    --gpu-memory-utilization 0.80 \
    --max-model-len 2048 \
    --max-num-seqs 64 \
    --enable-prefix-caching \
    --swap-space 4 \
    --block-size 16
```

`--max-num-seqs 64` (instead of default 256) to keep each instance's concurrent request count bounded on T4.

### 2.2 Workload parameters

**Chatbot workload (Part A):**
- 1.5B model on T4 sustains ~30–50 RPS per instance
- Target: 1500 reqs/cell, ~25 RPS/instance for 60s
- prompt = 128–256 tokens, output = 64–128 tokens (sampled per request)

**Agentic workload (Part A):**
- 40 workflows/cell, 4 instances → ~10 workflows/instance
- Steps ∈ {4, 8, 16} (chosen per run)
- Tool delay = 200 ms fixed (per request)
- Step generation: prompt 128 tokens, output 64 tokens
- Each step = 1 dispatch + 1 vLLM call + 1 tool sim

### 2.3 Pilot output constraints

- Chatbot pilot: target `running_count = 4-12` per instance (GPU util ~50-75% on T4)
- Agentic pilot: target `active_workflows = 6-12` per instance

---

## 3. Output Schema (extends Ver.2 §21)

```
~/B02/experiments/results/
    environment.json
    commit_hash.txt
    pilot_results/
        pilot_chatbot.json
        pilot_agentic.json
    part_a/
        run_<id>.json              # one per cell
        state_updates_raw.jsonl    # all state update records
        request_log.jsonl          # all per-request timing
        workflow_log.jsonl         # all per-workflow timing
        dispatcher_metrics.jsonl   # collection/serialization/merge timings
    part_b/
        run_<id>.json
        emulator_metrics.jsonl
    aggregates/
        state_size_summary.csv
        state_frequency_summary.csv
        part_a_real_serving_results.csv
        part_b_stress_test_results.csv
    figures/
        fig1_payload_size.png
        fig2_payload_ratio.png
        fig3_dispatcher_cpu.png
        fig4_p99_dispatch_latency.png
        fig5_state_traffic.png
        fig6_workflow_completion.png
    analysis_report.md
```

---

## 4. Threats to Validity (extends Ver.2 §20)

Ver.2 lists 6 threats. We add:

- **Loopback network underestimates transfer cost**: dispatcher and instances on same host, no real cross-machine latency
- **1.5B model does not exhibit preemption on T4**: `num_preemptions` likely degenerate; real production models at 7B+ behave differently
- **Wrapper state is fully owned by dispatcher**: in production, state may be distributed; centralization here favors correctness over realism
- **Tool execution is `asyncio.sleep(0.2)`**: no I/O, no real tool correctness; `tool_execution_status` is synthetic
- **Pilot run is single pilot per workload, not full sweep**: pilot-determined concurrency is approximate

---

## 5. Stop conditions

- All Part A cells complete with both reps passing quality filter (success_rate ≥ 0.95)
- All Part B cells complete
- All 9 tables and 6 figures generated
- `analysis_report.md` written

If a cell crashes 3× in a row, log to `failed_cells.json` and continue with other cells.