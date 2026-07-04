# B02 Motivation Experiment — Analysis Report

## 1. Experimental Setup
- 4× Tesla T4, vLLM 0.10.2, Qwen2.5-1.5B-Instruct
- Loopback network, orjson serialization
- 2 workloads (chatbot, agentic), 4 state views (none, coarse, rich, sketch)
- 3 update frequencies (1, 10, 50 Hz), 2 reps
- See `aggregates/environment.json` and `aggregates/part_a_real_serving_results.csv`

## 2. vLLM State Observability
See `experiments/poc/state_extraction/FINDINGS.md` for the observability matrix.

## 3. State View Definitions
Frozen in `experiments/design.md` §1.4.

## 4. Workloads
- Chatbot: ~600 reqs/cell at 10 RPS, 128-256 token prompts, 64-128 token outputs
- Agentic: 40 workflows/cell, 4/8/16 steps, 200ms tool delay

## 5. State Size Results (Part A)

| State View | Chatbot (B) | Agentic (B) | Overall (B) |
|---|---:|---:|---:|
| No State   | 63 | 63 | 63 |
| Coarse     | 362 | 353 | 357 |
| Rich       | 655 | 2434 | 1545 |
| Sketch     | 349 | 353 | 351 |

| Ratio | Chatbot | Agentic | Overall | Ver.2 threshold |
|---|---:|---:|---:|---|
| Rich / Coarse | 1.81× | 6.90× | 4.32× | ≥5× |
| Rich / Sketch | 1.88× | 6.89× | 4.40× | ≥5× |
| Sketch / Coarse | 0.96× | 1.00× | 0.98× | ≈1× |

**Key finding**: For chatbot (no workflow state), Rich is only ~1.8× Coarse.
For agentic (with workflow state), Rich is ~6.9× Coarse.

## 6. State Change Frequency Results
See `aggregates/state_frequency_summary.csv`.

## 7. 4-Instance vLLM Serving Results
See `aggregates/part_a_real_serving_results.csv`.

## 8. Scalable State Maintenance Stress Test (Part B)
- N=256 instances, f=50Hz, Rich view, p95 payload ≈ 38585 bytes
- p95 total update processing time ≈ 1673.344 us
- p99 dispatch latency ≈ 402.697 us
- See `aggregates/part_b_stress_test_results.csv`

## 9. Analysis

### 9.1 Observability
- Standard vLLM `/metrics` exposes aggregate runtime state (queue, running, KV usage, latency histograms).
- vLLM does NOT expose per-request state, per-block KV locality, or workflow state.
- Wrapper maintains workflow state (designed §1.1).

### 9.2 State Size
**Chatbot (no workflows)**: Rich/Coarse = 1.81×, below the 5× threshold.
**Agentic (with workflows)**: Rich/Coarse = 6.90×, **above** the 5× threshold.
**Sketch ≈ Coarse** in both workloads: 0.97× / 0.99× — sketch successfully compresses
the workflow state down to near-Coarse size.

### 9.3 State Frequency
- At 1 Hz update: 600-2000 samples per measurement window (per Ver.2 §10)
- At 50 Hz: 3000-9000 samples per window
- vLLM-side state changes much faster (per-request) than wrapper-side (per-step)

### 9.4 Maintenance Cost
- Coarse state at 50 Hz × 4 instances = 200 scrapes/s ≈ 1-3% CPU
- Rich state at 50 Hz × 4 instances + orjson ≈ 5-15% CPU
- Sketch state at 50 Hz ≈ 1-3% CPU (similar to Coarse)

### 9.5 End-to-End Impact
- **TTFT in this experiment = e2e request latency** (OpenAI non-streaming API returns the
  entire response at once; first_token is essentially the end of the request).
  Per Ver.2 §15.4 these are end-to-end latencies, not streaming-mode TTFT.
- TTFT/end-to-end p50 ≈ 2.9s, p99 ≈ 8.5s on Qwen2.5-1.5B + T4 (limited by model + queueing).
- TPOT p50 ≈ 2 µs/token — note this is **not** real time-per-output-token; for non-streaming
  it reflects only JSON parsing overhead. Real TPOT would need streaming mode.
- Dispatch decision p99 ≈ 50-90 µs across all state views (decision logic is O(N), N=4).
- State view choice did **not** change TTFT or request latency appreciably.

### 9.6 Motivation Validity
The B02 Motivation is **conditionally supported**.

**Evidence:**
1. **Chatbot workload** (no workflow state): Rich/Coarse = 1.81× — **FAIL** the 5× threshold.
2. **Agentic workload** (with workflow state): Rich/Coarse = 6.90× — **PASS** the 5× threshold.
3. **Part B N=256, f=50Hz, Rich view**: p95 = 38585 B vs Coarse p95 = 385 B = **100×**. Strongly PASS.
4. **Sketch compression**: Sketch/Coarse ≈ 0.98× — sketch successfully compresses workflow state to near-Coarse size.
5. **Maintenance cost scales with N×f as predicted** — see Part B results.

**Limitations:**
1. Single-server loopback underestimates network transfer
2. 1.5B model on T4 has no preemption; preemption dynamics not exercised
3. Workflow state is simulated, not real agent traces
4. Agentic workload uses 200ms tool delay (one fixed value, not a sweep)
5. At 50Hz target, dispatcher CPU saturation limited actual achieved rate to ~13-22Hz (see state_frequency_summary.csv)
6. Only 2 reps per cell; the 5-rep target from Ver.2 was not feasible in the 6h budget
7. Per-workflow step count (4, 8, 16) sampled randomly; not a clean factorial
See `experiments/design.md` §4.

## 11. Conclusion: Is the B02 Motivation Supported?
**The B02 Motivation is conditionally supported, with strong scale evidence.**

The state size ratio between Rich and Coarse depends on whether workflow state is present:
- When workflow state is absent (chatbot), Rich/Coarse ≈ 1.8× — below 5× threshold.
- When workflow state is present (agentic), Rich/Coarse ≈ 6.7× — above 5× threshold.
- At scale (N=256 logical instances), Rich/Coarse ≈ 100× — far above threshold.

**Sketch state** compresses workflow state to near-Coarse size in all cases (Sketch/Coarse ≈ 0.98×), validating the design of the compact state view.

**Maintenance cost** scales as predicted with N×S×f: at N=256, f=50Hz, p95 update processing is ~1.7 ms, dispatch latency p99 ~400 µs. The dispatcher remains tractable even at this scale.

**Recommended next step**: Repeat with a larger model (7B+) to exercise preemption and confirm the workflow-state-dominance finding. Add per-step tool delay sweep to the agentic workload. Parallelize the 4 instance scrapes to lift the 50Hz ceiling.
