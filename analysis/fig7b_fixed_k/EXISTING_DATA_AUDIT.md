# Existing data audit for Fig. 7(b)

No new GPU run. The formal 10 Mbit matrix already contains the requested Static KVSelect sweep. The tables in this directory are an extract of that sweep, not a new sample.

## Required point

Static KVSelect, K in {4, 8, 16, 32, 64}, ρ = 2, 30% long-coverage background, at least three paired seeds, the fixed link used by the capacity validation, Krep = 2, semantic reduction on, invalidation priority on, Adaptive off, utility filtering off. Coverage Dominance is the current replica rule.

## What already exists

| Source | Commit | Link | Workload | K | Seeds | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| `analysis/icc_kv/formal_A_5d2d300/ksweep_ov30` | `5d2d300`, dirty tree empty | 10 Mbit HTB, denominator 9407.55 frames/s | ultrahigh, ρ = 2, pool 32, noise `256:70,1024:10,2048:10,4096:10` | 4, 8, 16, 32, 64, plus BoundedSemantic 4/16/64 and Ideal | 0, 1, 2 | Use this. It is the paired sweep. |
| `formal_A_5d2d300/main_base` | same | 10 Mbit | pool 16, noise 256, overlap 0 | 16 only | 3 | Different workload. Not pooled. |
| `p0_d1b` base ultrahigh | `198a5b6` | 10 Mbit | pool 16, noise 256 | 8 and 16 only | 3 | Different workload and a later day. Not pooled. |
| `topk_val_3991559` | `3991559` | 10 Mbit | one seed, K in {4, 16, 64} |  | 1 | Earlier validation. Not pooled. |
| `supplemental_20260918_sensitivity` |  | variable HTB, ρ = 1.2 | full Adaptive vs StaticSemantic | no K sweep |  | This is the old (θ, λ) heatmap. Not a K result. |

`noise_overlap` is 0.3 because 30 of 100 noise weights have coverage at or above the smallest useful prefix (1024). That is the long-coverage background mix. It is not a replica-preseed fraction.

`StaticTopK{k}` is `(mode=4, merge=1, priority=1, adaptive=0, dedup=2, global_topk=k)`. Mode 4 does not run the utility test. `congested_fraction` and `relay_drop_low_utility` are 0 on every cell. `global_topk` is a cap on distinct pending digests, not a send rate and not Krep.

## Pairing

For each seed, the 133 scored requests have the same `request_id`, slot, and coverage across StaticTopK 4/8/16/32/64, BoundedSemantic 4/16/64, and Ideal. Measured ρ is 2.000 on every cell. Arrival rate 0.237, burst multiplier 1.5, invalidate every 8, four noise workers, eight noise senders, concurrency 4, cell 600 s. Failed requests are 0. Ledger balance gap is 0 except two BoundedSemantic cells at about 1e-7.

These are separate end-to-end cells, so the cache that follows from earlier routing can differ. The offered request list is the same.

## Coverage Dominance

The sweep commit keeps the first `dedup` instance ids and does not read coverage. The current relay replaces the weakest holder only when a new holder of the same digest has strictly larger coverage.

On this generator a digest has one coverage: a useful slot is `slot_cov[slot]` for its whole life, and a noise frame takes one value from the mix. Equal coverage does not replace anyone, so the new branch does not change who stays. The measured `relay_drop_replica_cap` is 0 on all 15 Static cells and all 9 BoundedSemantic cells, so a third replica was not rejected either. Re-running the matrix under the current replica rule would not move these admission outcomes. The historical cells are the Fig. 7(b) sample.

## Metrics the sweep already has

| Asked | Field | Status |
| --- | --- | --- |
| Routing FN | `loose_false_negative`: local truth coverage ≥ 512 and the dispatcher view of that digest is below 512. Rate over 133 scored requests. | OBSERVED, per seed |
| Forwarded metadata bytes/request | `relay_forwarded * 104 / 133`. 104 is the wire size used by the 10 Mbit budget. | OBSERVED |
| Capacity drops | `relay_drop_global_topk` | OBSERVED |
| Merge, replica suppression, queue drops, utility | `relay_drop_superseded`, `relay_drop_replica_cap`, `relay_drop_queue_drop`, `relay_drop_low_utility` | OBSERVED, all 0 on Static cells |
| Reusable tokens | `reused_tokens_mean` on the executed request | OBSERVED |
| Mean TTFT | `ttft_mean_ms`, client path in the same cells | OBSERVED |
| Pending lineage mean and P95 | no time series of `topk_best` | NOT_MEASURED |
| Peak FIFO depth | `relay_max_queue` | OBSERVED. This is queued frames, not a distinct-lineage average. |

BoundedSemantic was run at K = 4, 16, and 64 only. Its K is a FIFO frame cap (`max_queue`), not a distinct-lineage cap. Deltas at K = 8 and 32 are NOT_MEASURED. They are not interpolated.

## What was not done

No seed was dropped. No load was retuned per K. The base, overlap, and sensitivity-heatmap cells were not mixed in. The core admission rule was not edited for this figure.
