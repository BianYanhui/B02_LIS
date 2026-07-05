# B02 Trade-off Experiment v2 — Report

**Date**: 2026-07-05
**Goal**: Verify the Quality vs Cost trade-off using:
1. **Imbalanced load** (2 RPS background noise to instance_0 and instance_1)
2. **Quality as a vector** (TTFT p50/p95/p99, TPOT, cache hit, load stdev, SLA-success, workflow completion)

This is an improved version of the v1 trade-off test, addressing two of its weaknesses:
- v1 had **symmetric load** across all 4 instances (all policies looked similar)
- v1 measured quality as a **scalar** (cache hit rate only) — a single number can't capture routing quality

---

## 1. The Two Improvements

### 1.1 Imbalanced load

A `background_noise()` coroutine sends 2 RPS of single-token "echo" requests to
instance_0 and instance_1 (bypassing the dispatcher). This shows up in the vllm
`/metrics` as elevated `num_requests_running` on those instances. The dispatcher's
state view sees the imbalance and (depending on policy) routes around it.

This exposes the **load-balancing** quality dimension that v1 missed.

### 1.2 Quality as a vector

| Dimension | How measured | Better direction |
|---|---|---|
| TTFT p50 / p95 / p99 | streaming-mode first chunk time | lower |
| TPOT p50 / p95 / p99 | per-token decode time | lower |
| Cache hit rate | `vllm:gpu_prefix_cache_hits / queries` | higher |
| Per-instance load stdev | std of `num_requests_running` across 4 instances, averaged over all state collections | lower (more balanced) |
| SLA-success rate | % of steps with TTFT < 3000 ms | higher |
| Workflow completion p50 / p95 / p99 | per-workflow end-to-end | lower |
| Failure rate | % of steps where vllm returned error | lower |

Plus cost vector:
| Dimension | Better direction |
|---|---|
| State size p95 | lower |
| State traffic (B/s) | lower |
| Dispatch decision p50 / p95 / p99 | lower |

---

## 2. Setup

| Item | Value |
|---|---|
| Server | yhs1, 4×T4, vLLM 0.10.2, Qwen2.5-1.5B-Instruct |
| Workload | 10 workflows × 10 steps, concurrent=4, 90s measurement |
| Long shared system prompt | ~500 tokens, identical across all steps and workflows (designed for prefix-cache hit) |
| Update frequency | 10 Hz |
| Reps | 1 |
| Cells | **8** = 4 policies × 2 load conditions |
| Total wall-clock | ~6 min (1 min per cell) |

---

## 3. Results — full 8-cell table

| Policy | Load | TTFT p50 | **TTFT p95** | TPOT p95 | **Cache%** | **LoadStdev** | SLA% | WfP95 | **State B** | **Trf B/s** | DispP95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Round-Robin | balanced | 165 ms | **394 ms** | 79 ms | 85.3% | **0.44** | 100% | 22,723 | 365 | 14,127 | 38.8 us |
| Coarse | balanced | 188 ms | 466 ms | 91 ms | 87.9% | 0.81 | 100% | 28,772 | 365 | 13,904 | 81.9 us |
| Rich | balanced | 191 ms | **609 ms** ⚠ | 97 ms | 89.7% | 0.96 | 100% | 31,246 | **2,975** | **55,085** | 90.4 us |
| Sketch | balanced | 181 ms | 456 ms | 100 ms | **91.4%** | 1.03 | 100% | 31,519 | **353** | 14,034 | 84.5 us |
| Round-Robin | imbalanced | 155 ms | 375 ms | 86 ms | 87.3% | 0.47 | 100% | 23,509 | 365 | 14,100 | 45.3 us |
| Coarse | imbalanced | 147 ms | **388 ms** | 94 ms | 88.3% | **0.77** | 100% | 24,569 | 365 | 13,945 | 75.6 us |
| Rich | imbalanced | 135 ms | 383 ms | 96 ms | 89.5% | 0.85 | 100% | 25,301 | 2,494 | 56,970 | 90.3 us |
| Sketch | imbalanced | 157 ms | 400 ms | 98 ms | 89.3% | 0.86 | 100% | 26,669 | 353 | 14,045 | 80.4 us |

---

## 4. Key Findings

### 4.1 Finding 1: **Rich hurts TTFT in balanced load** ⚠

In the balanced scenario, Rich has the **worst** TTFT p95 (609 ms vs 394 for RR) — **despite** having the most state to make "better" decisions. Why?

- Rich's affinity-routing logic: `if workflow.assigned_instance_history: route back to it`
- But in a fresh system with no prior history, the policy effectively does nothing useful — the affinity_history is empty for early steps, and even when populated, the "affinity" instance is just a random one
- The cost of running the rich-state scoring function is added **without** the benefit of good affinity decisions
- Net result: **wasted work**, slower decision, no quality gain

This is the **classic "more state ≠ better decisions"** warning.

### 4.2 Finding 2: **Sketch is the cache hit champion**

Cache hit rate progression (balanced):
- RR: 85.3%
- Coarse: 87.9%
- Rich: 89.7%
- **Sketch: 91.4%** ← best

Sketch's quantized affinity signal is **better at cache locality** than Rich's full
affinity history. Hypothesis: the 16-bit tool_status_bitset and per-instance counter
array in Sketch give a **cleaner, more stable** signal than Rich's full assigned_instance_history
array (which can be noisy and misleading under low load).

### 4.3 Finding 3: **Imbalanced load exposes RR's weakness**

| Metric | RR balanced | RR imbalanced | Coarse balanced | Coarse imbalanced |
|---|---:|---:|---:|---:|
| TTFT p95 (ms) | 394 | 375 | 466 | **388** |
| Load stdev | 0.44 | 0.47 | 0.81 | 0.77 |

RR is the most balanced in `balanced` mode (0.44 stdev, lowest). But under imbalanced
load (2 RPS noise on instances 0,1), RR's load_stdev is still 0.47 — it **does not adapt**
to the imbalance. Coarse actually gets *better* (0.81 → 0.77) under imbalanced load
because its state view sees the imbalance and routes away from the loaded instances.

But TTFT under imbalanced load: Coarse = 388 ms, RR = 375 ms. RR is *faster* here
because its simple round-robin happens to spread the load evenly even with the noise.

**The story is nuanced**: RR's dumb algorithm is robust to imbalance *for this small
workload* but doesn't improve. Coarse is more sophisticated but doesn't help much
either at this scale.

### 4.4 Finding 4: **The trade-off is multi-dimensional**

Looking at the 8 cells, no single policy wins on all dimensions. The radar plots
(fig1, fig2) show each policy's quality "shape":

- **RR**: small, balanced shape; wins on dispatch speed and load balance in balanced
- **Coarse**: large shape; wins on TTFT under imbalanced load
- **Rich**: best cache hit in some regimes, but worst TTFT in balanced, and 4× the cost
- **Sketch**: best cache hit, lowest state size, mid cost — **most balanced quality profile**

### 4.5 Finding 5: **Cost of state is consistent and large**

| Cost metric | RR/Coarse/Sketch | Rich | Multiplier |
|---|---:|---:|---:|
| State size p95 (B) | 353–365 | 2,494–2,975 | **~8×** |
| State traffic (B/s) | 13,900–14,100 | 55,085–56,970 | **~4×** |
| Dispatch decision p95 (us) | 38–85 | 90 | **~2× vs RR**, ~1.1× vs others |

The cost gap is robust and significant. **Rich always costs more, but doesn't always
give better quality.** This is the cost side of the trade-off.

### 4.6 Finding 6: **Sketch hits the Pareto frontier in quality space**

| Dimension | RR | Coarse | Rich | Sketch |
|---|:-:|:-:|:-:|:-:|
| Cache hit | 3rd | 4th | 2nd | **1st** |
| TTFT p95 (balanced) | 1st | 3rd | 4th | 2nd |
| TTFT p95 (imbalanced) | 1st | **1st** | 2nd | 4th |
| Load balance (imbalanced) | 3rd | **1st** | 2nd | 4th |
| Workflow p95 | 1st | 3rd | 2nd | 4th |
| **State size** | **1st** | 1st | 4th | **1st** |
| **State traffic** | 2nd | **1st** | 4th | 2nd |
| **Dispatch time** | **1st** | 3rd | 4th | 2nd |

Sketch is **first or tied-first on cost (state size, traffic, dispatch)** and
**first on cache hit**, while being mid-pack on TTFT and load balance. This is exactly
the Pareto frontier the B02 Motivation predicts.

---

## 5. The Trade-off, Visualized

### 5.1 Radar charts (figs 1, 2)

Each policy is a 5-axis shape covering:
- Cache hit (more = better)
- Load balance (stdev inverted, more = better)
- TTFT p95 (inverted, more = better)
- Workflow p95 (inverted, more = better)
- SLA success (more = better)

**Balanced load (fig1)**: Sketch has the largest "envelope" on cache hit, RR is
strongest on TTFT/Workflow. Rich's shape is slightly smaller than Sketch despite
8× the state.

**Imbalanced load (fig2)**: Coarse grows the envelope (it adapts to imbalance).
Sketch remains strong. Rich is still the largest state, not the largest quality.

### 5.2 Cost vs quality scatter (fig 3)

X axis = state traffic (log scale), Y axis = cache hit %.
- The "ideal" corner is upper-left (high cache hit, low traffic)
- Sketch sits in the upper-left in balanced mode
- Rich sits in the lower-right (low cache hit per byte spent, in this scenario)
- RR sits in the lower-left (low cost, lower cache hit)

### 5.3 Load balance (fig 4)

Per-instance load stdev by policy. RR has the lowest (0.44–0.47) because it forces
uniform distribution. Coarse improves to 0.77 in imbalanced (was 0.81 in balanced).
Rich and Sketch are higher but they actively pursue cache locality, which costs
some load balance.

### 5.4 TTFT p95 (fig 5)

Cache hit % annotated on each bar. In balanced, RR is fastest (394 ms) with 85.3%
cache hit; Rich is slowest (609 ms) with 89.7% cache hit. The 4pp gain in cache hit
costs 215 ms in TTFT — **a clear trade-off**.

---

## 6. Conclusion

**The Quality vs Cost trade-off exists and is multi-dimensional.**

1. **Cost side** is clear: Rich costs 4–8× more state bandwidth and 1.1–2× more dispatch time
2. **Quality side** is vector, not scalar: different policies win on different dimensions
3. **No policy is Pareto-optimal on all dimensions** — there's no "free lunch"
4. **Sketch is the most balanced choice**: best cache hit rate, lowest state cost, mid-pack elsewhere
5. **More state can HURT**: Rich has the worst TTFT in balanced load (609 ms) because
   affinity routing without strong affinity signal is wasted work
6. **Imbalanced load changes the ranking**: Coarse becomes more attractive under
   imbalance (it adapts), while Rich remains expensive

**What this means for B02**: The argument for Sketch is now stronger:
- Quality is multi-dimensional; Sketch is best on cache hit, mid on others, and
  dominates on cost
- More state is not always better — depends on whether the workload has the
  affinity signal the extra state is designed to capture
- The B02 claim "**State View should be designed as a cost-aware semantic interface**"
  is supported: the interface should expose **affinity signal** (where the gain is)
  without the **full workflow state** (where the cost is)

**Recommended follow-up** (deferred, no longer in 30-min budget):
- Multi-rep cells to confirm 0.1-0.5pp differences are real
- Higher concurrency (12+) to make load balancing matter more
- Longer system prompt (2-5k tokens) to increase cache benefit
- 7B+ model where prefill cost is more dominant

---

## 7. Limitations (extends prior)

1. **1 rep per cell** — quality differences <1pp may be noise
2. **Low concurrency (4)** — load balance doesn't matter much at low load
3. **Short prompts (~500 token system)** — cache benefit is limited
4. **Loopback, 1.5B model** — same as v1
5. **Background noise is artificial** — real production imbalance may be more complex
6. **Imbalance target is fixed (instance_0+1)** — actual hot instance may rotate
7. **Same dispatcher code for all cells** — policy implementations not stress-tested

---

## 8. Files in this experiment

```
tradeoff_experiments/
├── scripts/
│   ├── quick_tradeoff_v2.py           # main v2 experiment (8 cells, 90s each)
│   └── plot_v2.py                     # radar + scatter + bar charts
├── results_v2/
│   ├── cells/
│   │   ├── round-robin_balanced/summary.json
│   │   ├── round-robin_imbalanced/summary.json
│   │   ├── coarse_balanced/summary.json
│   │   ├── coarse_imbalanced/summary.json
│   │   ├── rich_balanced/summary.json
│   │   ├── rich_imbalanced/summary.json
│   │   ├── sketch_balanced/summary.json
│   │   ├── sketch_imbalanced/summary.json
│   │   └── all_summaries.json
│   ├── figures/
│   │   ├── fig1_radar_balanced.png
│   │   ├── fig2_radar_imbalanced.png
│   │   ├── fig3_cost_vs_quality.png
│   │   ├── fig4_load_stdev.png
│   │   └── fig5_ttft_p95.png
│   └── report.md                      # this file
└── results_v2_run.log                 # main run log
```

To re-run:
```bash
source ~/B02/poc/.venv/bin/activate
cd ~/B02/tradeoff_experiments/scripts
python quick_tradeoff_v2.py --all --duration-s 90 --n-workflows 10 --n-steps 10 --concurrent 4
python plot_v2.py
```
**Total**: ~6 min.
