# Fig. 6 reconstruction

This directory is the 2026-10 Fig. 6 evidence. Each round has its own driver, and that driver is the one that produced the tables next to it. `f6_d1b_coverage_dominance/` is the exception: it runs the coverage-dominance change in `experiments/4t4/net/gateway_relay_4t4.py`. `Dispatcher.choose` is still unchanged. The earlier drivers did not modify the relay.

The formal ICC matrix is still `docs/icc_formal/` and `experiments/icc_kv/`. This reconstruction does not replace it.

## What to cite

| Panel | Cite | Result |
| --- | --- | --- |
| Fig. 6(a) | `f6_a2/` | Case C. Real 1024→2048→4096 growth. Natural delivery does not merge. One frozen inflight window does: mean forwarded-update reduction 0.549, P95 visibility 16.84 s → 4.09 s. |
| Fig. 6(b) | `f6_b1/` | Conditional priority benefit. A real `reset_prefix_cache` on worker 0, then a timed relay replay. Natural queue is empty. Under a frozen 0.792 s ACK window the decision-level invalid route is 3/3 for FIFO and 0/3 for priority. |
| Fig. 6(c) | `f6_f1_formal_ac/` | Overlap reduction from a frozen `Krep=2` replay of a live FullSync stream. Not a live replica cap on the four-GPU service, and not a TTFT. |
| Fig. 6(d) | `f6_d1/` | Arrival-order Krep=2. Case A at load 0 when the hidden owners hold the longer prefix. Mean paired client TTFT gap 1813 ms, coverage loss 3072. The 61.44 ms model regret is not that delay. |
| Fig. 6(d) follow-up | `f6_d1b_coverage_dominance/` | Coverage-dominance Krep=2, same protocol, new seeds. Formal D1 mean paired client TTFT gap +1.57 ms, coverage loss 0, route change 0/4. Does not replace the `f6_d1/` measurement. |

Candidate figures (`fig6a_candidate.pdf`, `fig6b_candidate.pdf`, `fig6c_candidate_reduction.pdf`, `fig6d_candidate.pdf`) are internal. They are not the figure in the paper.

## What not to cite as the panel

| Path | Why it stays |
| --- | --- |
| `f6_p0/` | Offline priority smoke. Residency was stipulated. `source_tombstones_sent = 0` in the earlier formal trace is an empty sample. |
| `f6_p1/`, `f6_p2/` | Replica-cap routing from a model, before the measured TTFT in `f6_d1/`. |
| `f6_p3/` | Readiness pass. The HTB delay model is `MODEL_REJECTED`. |
| `f6_f1_formal_ac/` for Fig. 6(a) | Same-coverage supersession, about 21% of offered updates. It is not the extension-merge claim. Use `f6_a2/` for that panel. |
| `analysis/p0_pressure_realism/` | Why a natural block-event stream did not fill the indexer, and why a 10 Mbit/s story is not these ACK windows. |

## Labels

`OBSERVED` is a vLLM or relay timestamp from that run. `CONTROLLED_REAL_CACHE_INVALIDATION` is a developer cache reset. `TIMED_RELAY_REPLAY` repeats a captured event stream through the frozen relay. `READ_ONLY_ROUTING_DECISION` is `choose` without a request. `EXECUTED_VLLM_REQUEST` is a request that was sent. `MODEL_DERIVED` is the cost formula, not a latency.
