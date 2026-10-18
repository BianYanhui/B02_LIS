# F6-P0 feasibility audit

Date: 2026-10-18. Branch `f6-p0-fig6-feasibility`, parent `fff14ff`. No GPU. No change to `experiments/`, formal4t4 results, Run A, Run B, or the KVSelect decision rule.

This audit answers whether Fig. 6(b) and Fig. 6(d) can be attributed with the code and logs that already exist. The offline smokes that follow are CONTROLLED. They are not a formal matrix.

## What the code actually does

### Gateway and `K_TOMB`

`experiments/4t4/net/gateway_relay_4t4.py`

- `K_TOMB` is kind 2. Coverage on the wire is 0. There is no opcode that shrinks the same digest in place.
- `--priority`: a tombstone is appended to `pqueue`. `_dequeue` pops `pqueue` before the FIFO. This is non-preemptive: a frame already handed to the kernel is not pulled back. Tombstones also skip the bounded-queue tail drop.
- `--dedup N` does not apply to tombstones. On forward, `_forget_replica` removes that instance from the holder set.
- If merge is on, a tombstone also marks a queued upsert for the same `(instance, digest)` as superseded.
- Replica holders are not removed when an upsert is forwarded. A digest that has reached N holders stays capped until a tombstone (or a top-k / low-utility drop) forgets one of them.

### Invalidation priority is a flag, not a standalone published policy

`experiments/4t4/run_formal4t4.py` `POLICY_DEFS`

| Policy | merge | priority | dedup | adaptive | global top-k |
| --- | --- | --- | --- | --- | --- |
| FullSync | 0 | 0 | 0 | 0 | 0 |
| RateFIFO | 0 | 0 | 0 | 0 | 0 |
| LatestOnly | 1 | 0 | 0 | 0 | 0 |
| AgeCov-Greedy | 0 | 0 | 0 | 0 | 0 |
| StaticSemantic | 1 | 1 | 2 | 0 | 0 |
| Adaptive | 1 | 1 | 2 | 1 | 16 |

No published cell turns priority on while leaving merge, dedup, and top-k unchanged. StaticSemantic versus FullSync is not an invalidation-priority contrast. The flag itself can be set alone. The offline smoke does that. The frozen result tables do not.

### Dispatcher view and worker choice

`Dispatcher` in `run_formal4t4.py`

- The affinity view is `index[instance][digest] = coverage`, updated only by `apply_upsert` and cleared by `apply_tombstone`.
- `choose` first takes the least-loaded worker among all four (`native`). It then keeps workers whose view coverage is positive, sorts by coverage then load, and keeps the first `j`.
- It returns that affinity target only when `incremental_tokens / 50 - load_gap * 2 > 0.5` ms. Otherwise it returns `native` and records affinity as false.
- Equal coverage on the least-loaded worker therefore never takes the affinity branch. Hiding a replica still matters when the least-loaded worker is the one that was hidden.

### Four state sources, kept separate

| Source | Where it lives | What F6 may use it for |
| --- | --- | --- |
| vLLM residency | `vllm_cached_tokens` on the selected worker; `BlockStored` / `BlockRemoved` in P0-A3/A4/C1 | Physical hit or miss after the request. P0-A4’s 18 prefix tombstones were rebuilt offline. They were not frames this harness sent. |
| ShadowCache | `publish_state`: one upsert per completion; `K_TOMB` only if the shadow LRU evicts a previously advertised digest | Formal `source_tombstones_sent`. Capacity 104544. Baseline cells never filled it. |
| Dispatcher view | `Dispatcher.index`, filled from delivered frames | The affinity bit and `dispatcher_view_coverage_tokens`. |
| Logical workers | fan-in `__fanin_*` and background instance 16 | Not GPU residency. Not used in this smoke. |

`source_view_false_positive` compares the dispatcher view with ShadowCache `source_truth`. `physical_false_positive_affinity` compares an affinity decision with `vllm_cached_tokens` on the selected worker. A plain cache miss is the second column only when affinity was true and advertised coverage was at least 512. The harness does not log a BlockRemoved timestamp next to that decision.

### Owner validation

The native check under `analysis/formal4t4/raw/native_validation/` exercises the developer endpoint (scope, lease, epoch, touch, free). It is an owner-side microbenchmark. It is not joined to gateway receive time, dispatcher apply time, or a routing decision. Formal request rows do record `validation_result` from cached tokens after the call (`cache_reused`, `cache_miss`, `fallback_prefill`). That is post-hoc on the selected worker.

### Replica cap = 2

`_admit_replica`: if the instance is not already a holder and `len(holders) >= dedup`, the upsert is dropped as `replica_cap` / `duplicate_holder`.

Retention rule, from the code:

- The first holders to enqueue are kept. Instance id is not special, except background instance 16, which must not take a slot.
- Load is not an input. Coverage is not an input. The coverage top-k is a different map.
- The holder set survives forwarding. Later upserts from a third owner stay dropped until a tombstone forgets a holder.

Overlap preseed (`run_formal4t4.py` around the E2 comment) sends each seed prefix to all four vLLM ports, then calls `publish_state` for instances 0, 1, 2, 3 in that order. With `dedup=2`, instances 2 and 3 are the ones the cap drops if the relay sees that order. Those two instances did receive the preseed request. Whether the prefix was still resident at a later decision is not in the replica-drop counter.

The dispatcher has no second channel that restores a dropped owner. Heartbeats in the fan-in supplement are a different experiment.

### Routing FP / FN and replay

Request rows already have `source_view_false_positive`, `source_view_false_negative`, `physical_false_positive_affinity`, `candidate_hit`, `selected_instance`, `advertised_owners`, and `vllm_cached_tokens`.

`tombstone_delay_p95_s` is the 95th percentile of delivered tombstone delays. `percentile()` returns 0 when the list is empty. A 0 in the baseline table means no delivered tombstone sample. It does not mean the delay was zero.

`experiments/icc_kv/replay.py` maps synthetic evict rows onto `K_TOMB` and fabricates digests. It does not replay P0-A4 block hashes into `Dispatcher.choose`. There is no reusable trace that aligns event time, gateway receive, gateway forward, dispatcher apply, and the routing decision for one real eviction.

## Existing numbers (OBSERVED, formal4t4)

From `analysis/formal4t4/summary/signaling_accounting.csv`. Each policy has 8 cells (two workloads, four ρ values). The figures below average those cell means. Every policy’s `source_tombstones_sent_mean` is 0.

| Policy | superseded mean | duplicate_holder mean | forwarded mean |
| --- | ---: | ---: | ---: |
| FullSync | 0 | 0 | 117.45 |
| RateFIFO | 0 | 0 | 37.45 |
| LatestOnly | 37.78 | 0 | 100.90 |
| AgeCov-Greedy | 0 | 0 | 118.90 |
| StaticSemantic | 30.95 | 25.15 | 93.23 |
| Adaptive | 15.18 | 17.68 | 79.95 |

`analysis/formal4t4/summary/correctness.csv`

- Baseline, every policy: `physical_false_positive_affinity_rate_mean` is 0, `tombstone_delay_p95_s_mean` is 0, `forced_owner_resets_mean` is 0.
- Churn is injected resets, not shadow eviction and not BlockRemoved. Adaptive (priority on, plus merge, dedup, top-k) has physical false-positive rate 0.040 and tombstone p95 about 170 s. LatestOnly (priority off, merge on) has physical false-positive rate 0.008 and tombstone p95 about 179 s. FullSync and RateFIFO record tombstone p95 as 0. StaticSemantic is absent from the churn rows. These rows do not isolate priority.

`supplemental_20260920_controlplane/` measures fan-in path delay under a 200 frame/s drain and 40 ms netem. Policies are still bundles. It is not a stale-positive experiment.

## Can the requested timelines be aligned?

| Event | Present today | Aligned to one routing decision |
| --- | --- | --- |
| State generation time | Upsert `t_send`; shadow insert time | For upserts that were delivered. No baseline tombstone. |
| Gateway receive / forward | Relay JSON `enqueued` / `forwarded` | Not joined to P0-A4 BlockRemoved. |
| Dispatcher apply | `received_at` on upsert; tombstone deletes the key | No per-tombstone apply time in the baseline, because no tombstone was sent. |
| Routing time, selected worker, advertised coverage | Request CSV | Yes, for the formal trace. |
| Actual reusable coverage | `vllm_cached_tokens` on the selected worker only | After the request. Not a residency snapshot of the other three workers. |
| Owner-validation result | Cached-token label; separate native microbenchmark | The microbenchmark is not on this timeline. |

A real stale-positive, as defined for this round, needs all three at once: the worker has lost the prefix, the dispatcher still has a positive affinity entry, and the request is routed because of that entry. The formal baseline has the columns and none of the tombstones. P0-A4 has the evictions and none of the routing decisions. Joining them after the fact is not possible. The missing observation is a single run that logs BlockRemoved (or a shadow tombstone that has been checked against cached tokens), gateway enqueue/forward, dispatcher apply, and the next `choose` for that digest, with only the priority flag changed.

## Verdict of the audit, before the smoke

Fig. 6(b) cannot be read out of the frozen tables. The mechanism can be isolated in a new configuration. The real stale window was not recorded.

Fig. 6(d) has an observed advertisement reduction (`duplicate_holder` on StaticSemantic and Adaptive) and a known retention rule (first two enqueued instances). It does not have a per-candidate load snapshot, so routing regret on the formal trace is NOT_MEASURED. A same-snapshot counterfactual is possible offline. That smoke is in `F6_D_SMOKE_REPORT.md`.
