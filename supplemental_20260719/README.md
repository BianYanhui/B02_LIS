# B02 Supplementary Live Experiments — 2026-07-19

Target paper: *Cost-Aware State Interfaces for LLM Request Dispatch* (Minimal State Sketch).

All runs execute on yhs1 (4× Tesla T4, one vLLM instance per GPU, Qwen2.5-1.5B-Instruct,
vLLM 0.10.2), reusing the frozen V5 live harness `supplemental_20260715/run_live_k_tradeoff_v5.py`
and its V4 dispatcher core `run_fixed_prompt_t4_replay_v4.py`. Stock files are untouched.

## Why these experiments (gap analysis)

The paper's live evidence (Section 4.3, Figure 4) rests on a **single workload point**:
96 active prefixes, Zipf α=0.55, 2048-token prefixes, concurrency 4, J=4. The paper itself
lists as limitations that (i) admission should adapt K to demand concentration, and
(ii) the harmful K=4 point motivates an abstention mode — but neither is backed by any
live experiment today. Section 4.4's queue–affinity conflict evidence is modeled
dispatcher replay only.

## Experiments

| ID | What changes | Run dir | Question |
|---|---|---|---|
| S1a | Zipf α=0.05 (near-uniform demand) | `live_alpha_uniform_a005/` | Does the K-quality curve (and K=4 harm) survive flat demand? |
| S1b | Zipf α=1.35 (heavy skew) | `live_alpha_skew_a135/` | Does small K suffice under concentrated demand? |
| S2 | Guard ablation at α=0.55: affinity-first vs abstention, K∈{4,16} | `live_guard_ablation/` | Live evidence that an abstention mode is needed and sufficient (Eq. 10, §4.3 abstention remark, §4.4 modeled conflict) |
| S3 | Concurrency 8 (vs 4) at α=0.55, K∈{4,16} | `live_concurrency8/` | Does the K=16 TTFT benefit survive heavier queueing? (conc=12 attempted 4×: EngineCore IMA/cuBLAS crashes on this T4 build — see §5 of SUPPLEMENTARY_EXPERIMENTS.md) |

Common protocol: 12 paired repetitions, byte-identical trace per rep across policies,
192 requests (64 warm-up + 128 measured), cache_salt isolation per (rep, policy),
fixed 4 output tokens, greedy decoding, vLLM servers restarted before every run to
flush residual KV (cache-namespace hygiene).

S2 uses `run_live_guard_ablation_v1.py` (new, this directory): subclasses the V4
`Dispatcher` with `affinity_first` and `abstain` guard modes. Abstention rule:
fall back to the native load decision unless incremental coverage ≥ 1024 tokens
**and** the affinity owner is not strictly busier than the native choice.

## Analysis

`analyze_supplements_v1.py` builds `analysis/combined_metrics.csv` and
`analysis/combined_summary.md`, computing the paper's R_inc (Eq. 11) per paired
repetition with 95% bootstrap CIs, paired TTFT deltas vs Load-Only, index bytes,
and guard hit/abstain rates. The V5 primary run (α=0.55) is included as the anchor.
