# Fig. 6(a) and Fig. 6(c) existing-data audit

No experiment was re-run. Nothing under `analysis/formal4t4/` was modified.

## Fig. 6(a) — extension aggregation

The gateway merge is `_supersede_queued_up` in `gateway_relay_4t4.py`. A newer upsert or a tombstone for the same `(instance, digest)` marks the queued unsent frame as superseded. The counter is `relay_drop_superseded`.

This is a gateway pending-queue replacement. It is not the vLLM path that packs many block hashes into one `BlockStored`. P0-C1’s collapse from 6615 hash occurrences to 102 prefix records is upstream of this counter and must not be added to the gateway merge benefit.

OBSERVED, mean of the 8 formal cell-means in `signaling_accounting.csv`:

| Policy | merge flag | superseded mean |
| --- | ---: | ---: |
| LatestOnly | 1 | 37.78 |
| StaticSemantic | 1 | 30.95 |
| Adaptive | 1 | 15.18 |
| FullSync, RateFIFO, AgeCov-Greedy | 0 | 0 |

The zeros on the merge-off policies are the right negative control for “the counter is the gateway rule.” They are not a measurement of zero extension opportunities in the trace.

The merge key does not require the new coverage to be larger. A repeated advertisement of the same digest counts the same as a growing extension. The summary CSV does not split those two. Calling the whole superseded count “extension aggregation” over-names it. The count itself is a real gateway-side merge opportunity and can be kept for a message-reduction plot.

These cells do not show that the merge changed the selected worker or the prefill. That claim is out of scope for this round and is not supported by the superseded column alone.

## Fig. 6(c) — replica suppression efficiency

The matching counter is `relay_drop_duplicate_holder`, incremented only by `_admit_replica`.

OBSERVED, same 8-cell average:

| Policy | dedup | duplicate_holder mean | forwarded mean |
| --- | ---: | ---: | ---: |
| StaticSemantic | 2 | 25.15 | 93.23 |
| Adaptive | 2 | 17.68 | 79.95 |
| LatestOnly and the other dedup-off policies | 0 | 0 | see signaling file |

The cap and the forward count come from the same cells. That part of Fig. 6(c) can be kept.

Shared definition with Fig. 6(d): both use this holder set. The kept owners are the first instances to enqueue, not the least loaded and not the longest coverage. Holders remain after the frame is forwarded. Formal baseline tombstones are 0, so those holders are not released later in the cell.

The workload that produces the drops is the formal trace at replica overlap 0.25, including the four-way vLLM preseed. `analysis/formal4t4/report/final_report.md` says the separate 0/25/50/75% overlap sweep is calibration-only and is not for the paper. Do not promote that sweep into Fig. 6(c).

Fig. 6(c) does not contain routing regret. The regret table for the new subplot is the CONTROLLED file `smoke_d/replica_regret.csv`. The two subplots can share the admission rule. They cannot share a single measured regret number, because the formal logs never stored the hidden owner’s load.

## Keep or rerun

Keep the formal superseded and duplicate-holder columns, with the names above. Do not rerun Fig. 6(a) or Fig. 6(c) to reproduce those counts. Do not treat upstream block aggregation, the calibration overlap sweep, or the CONTROLLED regret as if they were already inside those columns.
