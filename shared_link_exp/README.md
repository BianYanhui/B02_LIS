# Shared-link experiment: hybrid live evidence

Paper question: LLM serving instances advertise KV-prefix affinity state to a
request Dispatcher over a **capacity-limited shared control link**; queueing on
that link makes state stale and degrades dispatch.  The `sim/` subdirectory
holds the pure-simulation study.  This directory adds the **hybrid** version:

- **Real**: vLLM 0.10.2 inference on 4x Tesla T4 (one server per GPU, ports
  8000-8003, `--enable-prefix-caching --enable-prompt-tokens-details
  --max-num-seqs 8 --enforce-eager`), real KV-prefix state, real TTFT,
  physical `vllm_cached_tokens` telemetry per response.
- **Simulated**: only the shared control link.  Every state advertisement is a
  real 64-byte message (instance, prefix digest, coverage = prompt tokens,
  seq) that enters an in-process async byte-rate FIFO (serialization =
  64/B seconds per message, one shared link for all instances).  The
  dispatcher's affinity index is updated **only** when a message finishes
  "transmission".

## Files

- `run_live_shared_link_v1.py` — harness (self-contained, reuses the V4
  replay patterns from `supplemental_20260715/run_fixed_prompt_t4_replay_v4.py`).
- `restart_t4_shared_link.sh` — cluster launcher (adapted from the V4 restart
  script; kills only the B02 venv's vllm processes, which is authorized).
- `server_logs/` — vLLM logs; the per-instance GPU KV cache size in tokens is
  read from here and passed to the harness as `--kv-cache-tokens`.
- `live_v1/` — run artifacts: `run.log`, `traces/`, `results/`.

## Policies

| policy | behavior |
|---|---|
| `ideal` | index updated synchronously at request completion (no link); upper bound |
| `exact_fifo` | every ad/tombstone through the link, no filtering |
| `local_topk` | instance advertises only its current top-8 prefixes by coverage (recency tie-break); tombstones always pass |
| `agg_static` | pre-link aggregator: merge-superseded upserts (a newer ad for the same (instance,digest) cancels a queued unsent older one), replica cap 2 per digest across instances (drop excess upserts), tombstones jump to a priority lane served first (non-preemptive) |
| `agg_adaptive` | `agg_static` + drop upserts whose utility `U = exp(-(age+Dq)/tau)*coverage - lambda*64 <= 0` once EWMA queueing delay `Dq > 50 ms` (tau = 30 s, lambda = 28 tokens/byte, so with 2048-token coverage the drop fires only when age+Dq exceeds ~4 s — i.e. only under real congestion) |

Cells: `ideal` (B-independent) + 4 policies x 3 link capacities B chosen for
link utilization ~= 0.3 / 0.7 / 0.95 (calibrated from the raw advertisement
rate measured in a smoke run with `ideal`) = 13 cells per rep, >= 3 paired
reps.  All cells in a rep share the same request trace (identical seeds,
verified by trace SHA-256 and per-request prompt SHA-256); vLLM `cache_salt`
isolates KV namespaces between cells.

## Workload

Zipf alpha=0.55 over a 64-prefix active pool, 2048-token prefixes, 4-token
fixed outputs, closed-loop concurrency 4.  At 50% of requests the active pool
rotates to a **disjoint** 64-prefix slice (128 distinct prefixes total),
creating a correlated create+evict burst that stresses the link.  Per rep:
>= 32 warmup + >= 96 measured requests per cell.

## Shadow-model approximation (read this)

Tombstones ("prefix evicted") cannot be observed from vLLM's API, so they come
from a **per-instance LRU shadow model** of vLLM's block pool: inserts are
tracked in request-completion order, each prefix occupies its full prompt
length in tokens, and capacity is the REAL per-instance GPU KV cache size in
tokens read from the server logs (not assumed).  Known approximations:

1. The physical pool also caches shared leading blocks across prefixes and
   manages 16-token blocks; the shadow tracks whole prefixes.
2. Each cell's shadow starts empty, while the physical pool carries LRU
   residue from earlier cells (cache_salt namespaces are never flushed), so
   physical eviction of the current cell's entries can happen slightly
   earlier than the shadow predicts.
3. vLLM's real eviction is per-block LRU with refcounts, not per-prefix.

The shadow model only decides **when to emit tombstones**.  Ground truth for
reuse is ALWAYS the physical `vllm_cached_tokens` field in each response.  A
**stale fallback** is physical: the dispatcher routed to instance i expecting
coverage >= 512 tokens from its index, but the response shows
`cached_tokens < 512`.

## Metrics per cell

mean/p50/p95 TTFT, total physical cached tokens, saved-prefill retention vs
`ideal` (paired per rep), physical stale-fallback rate, ad queueing-delay
p50/p95, tombstone delivery delay p95, bytes/messages sent, aggregator drop
counters (superseded / replica cap / low utility), plus V4-style integrity
checks (byte-identical prompts and input token counts per logical request
across cells, fixed output length, usage telemetry present).
