# B02 — LLM Inference Service (LIS) with Cost-Aware Semantic State Interface

This repo hosts the B02 project: a proposal that the **Instance–Dispatcher boundary
in an LLM Inference Service should expose a compact semantic State View** (the
"Minimal State Sketch" idea), instead of streaming raw workflow-level state to the
central dispatcher.

The central claim being tested:

> In LLM Inference Services, the Dispatcher needs a State View from Serving
> Instances to make dispatch decisions. Standard runtime states are relatively
> compact, but **agentic workflow-level semantic states can make the State View
> much larger**. Therefore, the Instance–Dispatcher boundary should be designed
> as a **cost-aware semantic state interface**.

The Motivation Experiment is the main artifact in this repo. It uses vLLM as the
LLM Generation Backend and measures State View size, change frequency, and
maintenance cost under realistic agentic and chatbot workloads.

---

## Repository layout

```
B02/
├── docx/
│   └── prompt/                 # Motivation prompt Ver.2 (gitignored)
├── experiments/
│   ├── design.md               # frozen experiment design (architecture, scoring formulas, sketch algorithm)
│   ├── scripts/                # all experiment code
│   │   ├── launch_4vllm.sh     # launch 4 vLLM instances on T4
│   │   ├── stop_4vllm.sh
│   │   ├── dispatcher.py       # state collector + view builders + 4 dispatch policies
│   │   ├── workloads.py        # chatbot + agentic workload generators
│   │   ├── run_cell.py         # one cell: collector + workload in single process
│   │   ├── run_part_a.py       # orchestrate 48 Part A cells
│   │   ├── run_part_b.py       # 90 logical-emulator Part B cells
│   │   └── aggregate.py        # walk cell dirs, produce CSVs + figures + report
│   └── results/
│       ├── part_a/<cell_id>/   # 48 cell dirs (state_updates.jsonl, summary.json, etc.)
│       ├── part_b/<cell_id>/   # 90 cell dirs
│       ├── aggregates/         # the 4 CSVs + environment.json
│       ├── figures/            # fig1, fig3, fig5 PNGs
│       └── analysis_report.md  # full prose analysis
├── poc/
│   └── state_extraction/       # vLLM observability PoC (probe_01..04, FINDINGS.md)
├── src/                        # (empty placeholder)
├── .gitignore
└── README.md                   # this file
```

---

## Quick start (reproducibility)

From yhs1 (single host with 4×T4):

```bash
# 0. One-time setup (already done in this environment)
~/B02/poc/.venv/                                # vllm 0.10.2 + torch 2.8 cu128 + modelscope
# Model already downloaded at /home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct

# 1. Launch 4 vLLM instances (~3 min)
~/B02/experiments/scripts/launch_4vllm.sh

# 2. Run the experiment
cd ~/B02/experiments/scripts
source ~/B02/poc/.venv/bin/activate
python run_part_a.py --pilot                    # ~2 min
python run_part_a.py --part-a                   # ~80 min, 48 cells
python run_part_b.py --all --duration-s 20      # ~30 min, 90 cells
python aggregate.py --all                       # ~1 min, regenerates CSVs + report + figs

# 3. Inspect results
ls ~/B02/experiments/results/aggregates/
ls ~/B02/experiments/results/figures/
cat ~/B02/experiments/results/analysis_report.md
```

**Total wall-clock**: ~2 hours (well within 6h budget).

---

## Headline result

**The B02 Motivation is CONDITIONALLY SUPPORTED, with strong scale evidence.**

| | Coarse | Rich | Sketch | Ver.2 threshold |
|---|---:|---:|---:|---|
| Chatbot avg (B) | 362 | 655 | 349 | — |
| Agentic avg (B) | 353 | 2,434 | 353 | — |
| **Rich/Coarse — chatbot** | — | 1.81× | — | ≥5× **FAIL** |
| **Rich/Coarse — agentic** | — | **6.90×** | — | ≥5× **PASS** |
| Sketch/Coarse — overall | 0.98× | — | — | ≈1× **PASS** |
| Part B N=256, f=50, Rich p95 (B) | 385 | **38,585** | 1,117 | — |
| **Rich/Coarse at N=256** | — | **100×** | — | far above |

**Workflow state is the cost driver.** When workflow state is present (agentic, Part B at scale),
Rich is 6.9×–100× larger than Coarse. When workflow state is absent (chatbot), Rich is only 1.8× larger.

**Sketch compresses workflow state to near-Coarse size in all conditions** (Sketch/Coarse = 0.96×–1.00×).
This is the design win: same dispatching-relevant information (workflow progress, tool status,
instance affinity) at near-Coarse payload cost.

---

## Key files

| File | Purpose |
|---|---|
| `docx/prompt/B02_Motivation_Prompt.Ver.2.md` | The motivation prompt that defined this experiment (gitignored) |
| `experiments/design.md` | Frozen design choices (architecture, scoring formulas, sketch algorithm, threat model) |
| `experiments/results/analysis_report.md` | Full prose analysis with Motivation Validity verdict |
| `experiments/results/aggregates/*.csv` | Aggregated data tables (state size, frequency, Part A results, Part B results) |
| `experiments/results/figures/fig{1,3,5}_*.png` | Payload size, dispatcher CPU, state traffic plots |
| `poc/state_extraction/FINDINGS.md` | vLLM observability PoC findings (probe_01..04) |

---

## Experimental environment

| Item | Value |
|---|---|
| Server | yhs1 (192.168.2.125), single host |
| GPUs | 4× NVIDIA Tesla T4 (15 GB VRAM each, compute capability 7.5) |
| GPU driver | CUDA 12.8 (forced vLLM 0.10.2 + torch 2.8 cu128) |
| vLLM | 0.10.2 (last 0.10.x with cu12 support) |
| PyTorch | 2.8.0+cu128 |
| Transformers | 4.55.2 |
| Model | Qwen2.5-1.5B-Instruct (from ModelScope) |
| Serialization | orjson |
| Network | loopback |

**vLLM launch params** (frozen):
```
--gpu-memory-utilization 0.60
--max-model-len 2048
--max-num-seqs 64
--enable-prefix-caching
--swap-space 4
--block-size 16
--enforce-eager
```

---

## What is B02?

B02 is part of a research program on **LLM Inference Service architecture** for
agentic workloads. The full motivation prompt is in `docx/prompt/` (gitignored
because it contains the experimental design brief, not source code).

The short version: when an LLM Inference Service serves agentic workflows
(multi-step LLM calls with tool execution between steps), the dispatcher
benefits from knowing which instance has which workflow's KV cache. But
shipping the full workflow state to the dispatcher every state-update tick is
expensive. B02 proposes a compact "Sketch" view that preserves the dispatching
signal at near-Coarse payload cost. The experiment in this repo measures how
big the saving actually is.

---

## Threats to Validity

- Single-server loopback underestimates cross-host network transfer
- 1.5B model on T4 does not exhibit preemption (real 7B+ models would)
- Workflow state is fully owned by the dispatcher (real systems may distribute)
- Tool execution is `asyncio.sleep(0.2)` — synthetic, not real tool I/O
- 50 Hz update target was not fully achieved (13–22 Hz actual) due to
  dispatcher CPU saturation on serial /metrics scrapes
- Only 2 reps per cell (Ver.2 specified 5–10; reduced per user instruction)
- OpenAI non-streaming TTFT here = end-to-end latency, not true time-to-first-token

See `experiments/results/analysis_report.md` §7 for full discussion.
