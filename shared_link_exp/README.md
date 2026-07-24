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

## v2 (run_live_shared_link_v2.py)

v1 findings drove three changes:

1. **Order de-confounding**: v1 ran cells in fixed order within each rep while
   a global TTFT drift aliased policy order with time.  v2 shuffles the
   per-rep cell order with a per-rep seed (`cell_order_index`,
   `cell_order_sequence`, and `cell_order_by_rep` in run metadata).
2. **Coverage-growth workload (agentic lineage)**: v1's fixed 2048-token
   coverage meant merge-superseded never fired.  v2 lineages are 3-step
   chains; step k's prompt is step k-1's prompt plus a ~512-token extension
   (measured input tokens 2094 / 2617 / 3140; advertised coverage target
   2048 / 2560 / 3072).  Step prompts are literal string prefixes, so
   physical prefix caching chains along the lineage.
3. **Deeper congestion**: v1's tightest tier (util ~0.95) only reached ad
   queue p95 ~0.9 s.  v2 tiers target util ~0.5 / 1.0 / 1.75 of the raw ad
   rate remeasured under the lineage workload (~96 B/s upserts, ~110 B/s
   with tombstones; tiers 220 / 110 / 63 B/s).  exact_fifo (only) drops the
   oldest queued message beyond a backlog of 200 (metric
   `backlog_drop_count`); cell-end drain is capped at 60 s with the
   undeliverable remainder counted as drain overflow.

Pool sizing: 64 lineages per phase, 128 distinct total; per-instance working
set plus cross-cell cache_salt residue exceeds the real 104,544-token KV
pool, so physical evictions continue.  Added metrics: `backlog_drop_count`,
`link_max_backlog_depth`, `ad_queue_delay_mean_s`,
`mean_coverage_shortfall_tokens`, per-rep cell order.

## v3 (net/ + run_live_shared_link_v3.py): real kernel networking state channel

v1/v2 simulated the shared link as an in-process asyncio queue.  v3 moves the
state channel onto REAL kernel networking.  **Deviation from the letter of
the reviewer plan: vLLM stays on the host** (GPU simplicity); ALL signaling
and background traffic still traverses real kernel networking:

```
harness instance-agents (host) --TCP--> gateway container (b02-net, NET_ADMIN)
                                            |  tc HTB on eth0 egress
                                            |  class 1:10 signaling (dst port 9701)
                                            |  class 1:20 background (iperf3, dst port 5201)
                                            +--TCP--> dispatcher endpoint (host, bridge IP:9701)
```

- `net/gateway_relay.py` — TCP relay + semantic aggregator (alpine stdlib
  python3).  Fixed 64-byte binary frames (format documented in the file
  header).  Per-cell config frame selects mechanisms: merge-superseded,
  tombstone priority lane, cross-instance replica cap (--dedup 2), adaptive
  utility gate (EWMA delivery delay from real ack feedback).  Passthrough =
  exact_fifo.  The downstream socket has SO_SNDBUF pinned and a low asyncio
  high-water mark, so release blocks on real kernel backpressure; the
  relay's internal FIFO is the pre-kernel queue a real aggregator controls.
  Passthrough mode drops oldest beyond a 200-message queue (metric
  relay_drop_backlog_cap).
- `net/setup_net.sh` — idempotent: docker network `b02-net`
  (172.30.0.0/24), image `b02-gw` (alpine + iproute2 + iperf3 + python3),
  containers `gateway` (NET_ADMIN, 127.0.0.1:9700 published) and `bgserver`
  (iperf3 server :5201), tc HTB on gateway eth0 (parent = shared link;
  children guaranteed half, ceil = full link).  `net/teardown_net.sh`
  removes ONLY those.  Existing containers/networks are never touched.
- `net/cell_rate.sh --sig-bit N` — per-cell link rate; cells specify
  `--rho`, and the harness computes rate = offered/rho where offered is
  MEASURED during an ideal cell (msgs/s x 104 on-wire bytes).
- `net/tc_stats.sh` — `tc -s qdisc/class` snapshot; the harness stores
  before/mid/after snapshots per cell under live_v3/tc/.
- `net/restart_t4_v3.sh [LOW|MED|HIGH]` — vLLM cluster with parameterized
  gpu-memory-utilization (0.60/0.40/0.30); KV token capacity is read from
  the server logs, never assumed.
- `net/smoke_v3.sh` — end-to-end smoke with assertions: (a) real tc backlog
  at rho=1.3, (b) delivery p95 materially larger at rho=1.3 than 0.5,
  (c) ideal bypasses the network, (d) integrity checks, (e) iperf3
  background sharing raises signaling delay with both classes active.

Delivery delay is measured cross-process with wall-clock timestamps embedded
in each frame (same host => shared clock): delay = dispatcher receive time -
agent send time.  Ground truth for reuse remains the physical
`vllm_cached_tokens` per response; the shadow model only emits tombstones.
