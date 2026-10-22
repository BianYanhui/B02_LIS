# P0-D1 existing K-sweep audit

No new GPU run. The formal 10 Mbit matrix already contains a static K sweep. K=24 was never configured. `TOPK_SWEEP` in `experiments/icc_kv/wire.py` is `(4, 8, 16, 32, 64)`.

The gateway file at the run commit `5d2d300` matches `HEAD` for `experiments/4t4/net/gateway_relay_4t4.py`, `experiments/icc_kv/wire.py`, and `experiments/icc_kv/e2e.py`. The mechanism below is the one that produced these cells.

## What K constrains

`global_topk` is the maximum number of distinct pending digests in `topk_best`. It is not a forwarding rate.

The check is at admission, inside `Relay.enqueue`, before the frame joins the FIFO:

1. A digest already in the set is not rejected.
2. A new digest is rejected when the set is full and its coverage does not beat the current worst. Equal coverage keeps the smaller digest.
3. After a frame is accepted, `trim_global_topk` drops the worst digest's still-queued frames if the set is over the limit. That path matters when Adaptive shrinks the limit. Frames already written toward the socket stay there.
4. Forwarding is FIFO, except a priority tombstone already in `pqueue`. Top-K does not reorder admitted upserts by coverage.
5. When the last queued copy of a digest is forwarded or superseded, `_topk_release` removes it. A later update of that digest can enter again. A drop is not a permanent loss.
6. Replica suppression (`dedup=2`) runs after the Top-K check. A third replica never takes a slot. Merge replaces the queued copy of the same owner and digest; it does not free the digest for a different prefix.
7. Adaptive is `global_topk=16` with the adaptive flag on. While `congested()` is true the limit becomes `max(1, K // 4)`, so 16 becomes 4. Congestion latches on an ACK EWMA above 2 s, or on a live queue at or above 8 frames for 0.2 s. P0-B1 already recorded that this latch did not fire on the 10 Mbit Run B cells: `congested_fraction=0`, `low_utility=0`, while `global_topk` drops stayed in the hundreds of thousands to millions.

## Which runs exist

| Source | Commit | Link | Workload | K values | Seeds | Use |
| --- | --- | --- | --- | --- | --- | --- |
| `formal_A_5d2d300/ksweep_ov30` | `5d2d300` | 10 Mbit | ultrahigh, pool 32, ov30 | 4, 8, 16, 32, 64 | 3 | The only paired static sweep |
| `formal_A_5d2d300/main_base` | same | 10 Mbit | ultrahigh, pool 16, noise 256 | 16 only | 3 | Whether the cap hurts the base workload |
| `formal_B_5d2d300/rhoscan_base` | same | 10 Mbit | ρ 0.5–2.0, pool 16 | 16 only | 2 | When the cap fills |
| `formal_B_5d2d300/ablation_base` | same | 10 Mbit | ultrahigh and burst, pool 16 | 16, plus Adaptive | 2 | Shrink path did not latch |
| `topk_val_3991559` | `3991559` | 10 Mbit | one seed, K in {4, 16, 64} |  | 1 | Earlier validation. Not pooled with `5d2d300` |
| `e2e_full`, `ablation_c13000` | `61864db` | 1 Gbit | Adaptive, no K sweep |  |  | Different link and an older drop counter. Not used |
| formal4t4 | `1dc24ec` | variable HTB | Adaptive vs StaticSemantic |  |  | P0-B1. Not a K sweep |

K=24 is `NOT_MEASURED`. No cell is filled in by interpolation.

## Ov30 ultrahigh, three seeds

Same request identity: for every seed, the 133 scored requests match on slot and coverage across K=4, 8, 16, 32, and 64. These are separate end-to-end cells, so the cache that results from earlier routing can differ. The request list itself is the same.

| K | FN rate | Mean TTFT ms | Reused tokens | Foreground lag P95 | Peak queue | Top-K drops | Forwarded / s | Gateway CPU | Undelivered / 143 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 0.0251 | 1743 | 1433 | 1.9 ms | 5 | 8.31e6 | 4971 | 0.466 | 14.3 |
| 8 | 0.0075 | 1685 | 1453 | 2.8 ms | 9 | 6.57e6 | 7877 | 0.580 | 4.7 |
| 16 | 0.0301 | 1729 | 1440 | 12.3 ms | 17.7 | 5.20e6 | 10148 | 0.634 | 17.7 |
| 32 | 0.0125 | 1815 | 1435 | 13.3 ms | 33.3 | 5.21e6 | 10140 | 0.694 | 7.0 |
| 64 | 0.0150 | 1763 | 1446 | 15.4 ms | 65.3 | 4.91e6 | 10641 | 0.837 | 3.7 |

Sample sizes are three. FN counts are single-digit per cell. The TTFT sample SD inside one K is about 280–370 ms.

Peak queue sits on the cap for every K (`K` or `K+2`). The link is already full at K=16: forwarded rate is 10148 / s at K=16 and 10140 / s at K=32. A 10 Mbit link of 104-byte frames cannot carry much more than that. K=64 moves the rate only to 10641 / s and raises gateway CPU from 0.63 to 0.84.

## Base workload, K=16 only

Pool 16, every useful prefix 4096 tokens, noise 256. On main ultrahigh, three seeds, FN rate is 0. Undelivered foreground updates are 1, 3, and 1 out of 143. Peak queue is 18 or 19. Top-K drops are about 5e6.

On rhoscan, FN rate is 0 at every ρ from 0.5 to 2.0 (two seeds). Peak queue is already 16 at ρ=0.5, with only 552 and 586 drops. At ρ=2 the drops are about 5e6 and the forward rate is about 10k / s. The cap is occupied. The routing FN is not.

Undelivered updates and routing FN are different counts. On this base workload a few foreground updates are undelivered and no scored request is a loose false negative.

## What is not in the logs

The ksweep directory has the summary and the per-request CSV. It does not have the per-update gateway log. These are therefore `NOT_MEASURED`:

- lineages waiting in a 10 ms, 100 ms, or 1 s window
- time spent with the set full
- coverage of ranks 17–24 and 25–32
- whether a specific dropped update was unused, superseded, replicated, re-advertised, or the cause of one FN

Update-level harm is `COUNTERFACTUAL_NOT_VERIFIED`. The request table in `topk_harmful_drop_analysis.csv` only counts FN flips between two finished cells.
