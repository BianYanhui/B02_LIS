# Shared-Link Semantic State Propagation Simulator

Discrete-event simulator (Python stdlib only) for the B02 paper's re-framed
narrative: **network-aware semantic state propagation over a capacity-limited
shared control link**.

Serving instances continuously advertise KV-prefix affinity state
(create / extend / evict / restart events) to a central Dispatcher.  Full
reporting contends on a shared bottleneck link; queueing delay makes state
stale on arrival; stale state degrades KV-reuse dispatch.  An aggregator
placed *before* the bottleneck performs cross-instance dedup, merging of
superseded updates, and dynamic value/freshness/congestion-aware selection.

**Thesis under test:** under a bounded shared signaling channel,
fewer-but-fresher updates can beat a complete-but-stale state view.

## Files

- `shared_link_sim.py` — the simulator (single file, CLI, stdlib only).
- `run_all.py` — runs the full E1/E2/E3 grid in-process, writes `results/`.
- `../results/` — `e1_scaling`, `e2_freshness`, `e3_dispatch` (`.json`+`.csv`)
  and `summary.json` (headline numbers).

## System model

| Component | Model |
|---|---|
| Instances | `N` (default 128), each holding up to `R` (64) resident resources |
| Resource | `(digest, coverage_tokens)`; coverage init ~ U[512, 8192], extend adds U[512, 2048], cap 65536 |
| Popularity | global Zipf over `V` (4096) digests, exponent `alpha` (0.55) |
| Churn | per-instance Poisson, `churn_rate` (0.5) events/s; mix create 45% / extend 35% / evict 19% / restart 1% |
| Create-on-resident | becomes an extend (so Zipf-hot digests dominate both creates and extends) |
| Capacity eviction | create on a full instance evicts a uniformly random victim (emits a tombstone) |
| Restart | invalidates all of the instance's resources, `epoch++`, one 64 B epoch-invalidate message |
| Bursts | every `burst_period` (60 s) for `burst_len` (5 s): churn ×`burst_mult` (8); plus correlated mass eviction of `burst_evict_frac` (0.25) of every instance's resources at burst start; `--no_burst` / `--burst_evict_frac 0` to disable |
| Message size | 64 B for upsert, tombstone, and epoch messages |
| Shared link | single FIFO byte-server, capacity `B` B/s; transmission time = size/B; optional high-priority lane (strict, non-preemptive) |
| Dispatcher index | `digest -> {instance: (coverage, seq, gen_time, epoch)}`; upsert (seq-guarded) / tombstone / epoch-invalidate; epoch guard drops pre-restart stragglers |
| Requests | Poisson at `req_rate` (20/s), Zipf-sampled target digest; pick max-coverage advertised replica |
| Validity | hit is *valid* iff the chosen instance still truly holds the digest with matching epoch → `saved_prefill += min(advertised, true coverage)`; otherwise *stale fallback* (saved 0); no index entry → cold miss |

## Policies (`--policy`)

1. `ideal_exact` — infinite link capacity (zero queueing). Upper bound.
2. `exact_fifo` — every event enqueued, no filtering/merging.
3. `local_topk` — per-instance static top-`K` (default 8) by coverage;
   tombstones for previously advertised resources and epoch messages always
   emitted.
4. `agg_static` — pre-link aggregator: (a) merge superseded (a queued unsent
   older update for the same `(instance, resource)` key is cancelled);
   (b) cross-instance dedup: at most `max_replicas` (2) advertised replicas
   per digest, preferring largest coverage — a larger new replica displaces
   the smallest admitted one via an aggregator-generated withdraw-tombstone;
   (c) tombstones/epochs use a strict-priority lane.
5. `agg_adaptive` — `agg_static` + dynamic global budget: EWMA queueing
   delay `Dq`; upsert utility
   `U = exp(-(age+Dq)/tau) * coverage/8192 - lambda * size/64 - theta * Dq/dq_target`,
   `lambda = lambda0 * Dq/dq_ref`; `U <= 0` → drop (tombstones exempt);
   `tau` = EWMA of observed resource lifetimes (init `tau_init` 30 s);
   while `Dq > dq_target` (50 ms) the replica cap tightens to 1, relaxing
   when congestion clears.

## Metrics (per rep, after warmup; aggregated as mean ± 95% t-CI)

- `offered_Bps`, `tx_bytes`, `utilization` (link busy time / window)
- `q_max`, `q_p95` — link queue length in waiting messages (event-sampled)
- `qdelay_{mean,p50,p95}` — enqueue → transmission-start
- `age_delivery_{p50,p95}` — delivery time − generation time
- `tombstone_delay_p95` — generation → delivery for tombstone/epoch messages
- `age_use_p95` — request time − generation time of the index entry used
- `valid_hit_rate`, `cold_miss_rate`, `stale_fallback_rate`
  (= fallbacks / (valid hits + fallbacks))
- `saved_prefill_total`, `saved_prefill_mean` (per request)
- `msgs_dropped / merged / withdrawn / filtered` — aggregator/policy actions

Aggregation across reps: per-rep metric → mean and two-sided 95% Student-t
confidence interval (t table up to df=30, 1.96 above).

## Running

```bash
# full paper grid (reps=10, 120 s window + 20 s warmup, bursts on):
python3 sim/run_all.py

# quick smoke grid (~seconds):
python3 sim/run_all.py --quick --outdir results_smoke

# single experiment / single cell with overrides:
python3 sim/shared_link_sim.py --experiment e2 --e2_bs 8,64 --reps 3
python3 sim/shared_link_sim.py --experiment e3 --policy agg_adaptive --e3_bs 16 \
    --lambda0 0.1 --reps 10 --outdir /tmp/x
```

Defaults: `--N 128 --R 64 --V 4096 --alpha 0.55 --churn_rate 0.5
--req_rate 20 --B 64` (KiB/s) `--K 8 --max_replicas 2 --duration 120
--warmup 20 --reps 10 --seed 1`.  Repetition `i` of every cell uses seed
`seed+i` (common random numbers → paired comparison across policies).
Output JSON/CSV are byte-identical for identical seeds.

## Experiments

- **E1 scaling** (`e1_scaling`): `exact_fifo`, bursts on, B = 64 KiB/s,
  N ∈ {16, 32, 64, 128, 256}.  Offered load grows ~linearly in N;
  `q_p95`/`qdelay_p95` mark where burst-phase load crosses capacity.
- **E2 freshness vs utilization** (`e2_freshness`): `exact_fifo`, N = 128,
  bursts on, B ∈ {8, …, 256} KiB/s.  State-age-at-delivery and stale-fallback
  rate blow up non-linearly as utilization → 1.
- **E3 dispatch quality** (`e3_dispatch`): N = 128, bursts on, all 5 policies
  × B ∈ {16, 32, 64} KiB/s, plus `ideal_exact`.  At B = 16 KiB/s `agg_static`
  beats `exact_fifo` on saved prefill (≈96% vs ≈95% of ideal) with ~7× lower
  stale-fallback rate while sending ~15% fewer bytes; `agg_adaptive` cuts
  p95 queueing delay ~8× further at some retention cost (replica-cap
  tightening + utility drops under congestion).

## Documented simplifications

- Extend/evict choose a uniformly random resident resource; the Zipf bias on
  creates (create-on-resident ⇒ extend) already makes hot digests dominate.
- Capacity eviction picks a uniformly random victim (stands in for LRU).
- Aggregation is instantaneous; all queueing happens in the link queue(s).
- Strict-priority lane is non-preemptive.
- `local_topk` does not withdraw resources that fall out of the top-K
  (spec: only tombstones for previously advertised resources are emitted).
- The aggregator's replica view is exact w.r.t. what it has admitted (it sees
  every event).
- `ideal_exact` forces `B = ∞` regardless of `--B`.
- `--duration` is the measurement window; total simulated time is
  `warmup + duration`.
