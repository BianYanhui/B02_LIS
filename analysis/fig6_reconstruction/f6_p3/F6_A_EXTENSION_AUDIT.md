# F6-P3 Fig. 6(a) — Extension versus duplicate supersession

The frozen rule in `Relay._supersede_queued_up` replaces any unsent upsert for the same `(instance, digest)`. It does not require the new coverage to be larger. A superseded gateway row is therefore not, by itself, an extension merge.

Native block aggregation is not in these logs. Each row is one prefix advertisement. Only an update that was still in the gateway pending set is counted.

## Published cell means are not reconstructable

`analysis/formal4t4/summary/signaling_accounting.csv` has eight workload × rho rows for each policy. Their superseded means are:

| Policy | Superseded mean | Duplicate-holder mean | Rows |
| --- | ---: | ---: | ---: |
| LatestOnly | 37.775 | 0 | 8 |
| StaticSemantic | 30.950 | 25.150 | 8 |
| Adaptive | 15.175 | 17.675 | 8 |

These are the previously published cell means, not new measurements. Raw `gateway_events` CSVs exist for the dynamic `reuse_intensive` runs only. The baseline `original_compatible` runs and the other reuse rho rows have metadata and no event log. Those eight-row means are `NOT_RECONSTRUCTABLE`. They are not split into extension and duplicate here.

The dynamic files are a different corpus. Metadata `n_requests` is 320 and `pool_size * overlap = 16` prefixes are seeded on all four workers, which is `320 + 64 = 384` upserts. The accounting rows have 184 generated upserts (`120 + 64`). Do not substitute one corpus for the other.

## Classifier

Events stay in file order. A `suppressed / superseded` upsert is joined to the next log row.

- The previous update must still be the pending `(cell, owner, digest)` entry. Otherwise the row is `ANOMALOUS`.
- Next row is an upsert for the same key, with a new `update_id`: `EXTENSION` if coverage rises, `DUPLICATE` if it is unchanged, `ANOMALOUS` if it falls.
- Next row is a tombstone for the same key: `TOMBSTONE`. This is a churn cancellation, not an extension.
- Any other successor: `OTHER`. No coverage is guessed.

There is no separate version field. `update_id` is the sequence number.

## Reconstructable logs

| Corpus | Reps | Offered | Extension | Duplicate | Tombstone | Other | Anomalous |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dynamic LatestOnly | 5 | 384 each | 0 | 70, 74, 73, 94, 88 | 0 | 0 | 0 |
| dynamic StaticSemantic | 5 | 348–352 | 0 | 55–93 | 0 | 0 | 0 |
| churn LatestOnly | 1 file, 5 cells | 1560 | 0 | 239 | 896 | 0 | 0 |

LatestOnly rep 0, the pre-registered pairing log: 384 offered, 310 forwarded, 74 superseded, all `DUPLICATE`. True extension ratio 0. Total gateway-side supersession ratio `74/384 = 0.193`. Pending duration of the cancelled update was 3.55 s at p50 and 16.55 s at p95. The log balances: `310 + 74 = 384`. Nothing was left as an unexplained drop.

Across the five LatestOnly reps the superseded counts average 79.8 of 384 (ratio 0.208). Every one is a same-coverage repeat. `reuse_prefix_lengths` assigns a fixed 1024 / 2048 / 4096 coverage to each lineage, so a later advertisement of the same digest does not grow.

StaticSemantic shows the same coverage result, but replica cap and tombstone priority are also on. It is not the isolated merge measurement. Churn's 896 tombstone joins are owner resets. They are not Fig. 6(a).

## Merge on / off

Other mechanisms were off in the isolated comparison: invalidation priority, replica cap, adaptive utility, coverage top-k.

### A-low, observed dynamic LatestOnly rep 0

Source: `analysis/formal4t4/raw/dynamic/gateway_events_dynamic_LatestOnly_rep0_20260726.csv`.

Arrival span 380.1 s. Offered rate about 1.01 upsert/s. The phase file sets a controlled HTB rate of 1754 bit/s, then 730, then 1754. That is the experiment's shaped capacity, not a 10 Mbit/s path and not a production bottleneck. `104 * 8 / 1754` is 0.47 s per frame on paper.

| Arm | Evidence | Forwarded | Superseded | Extension | P95 latest-state visibility |
| --- | --- | ---: | ---: | ---: | ---: |
| Merge on | Observed log | 310 | 74 | 0 | 41.42 s |
| Merge off | Accounting identity | 384 | 0 | 0 | not assigned |
| Merge on | HTB replay | 363 | 21 | 0 | 5.09 s |
| Merge off | HTB replay | 384 | 0 | 0 | 5.78 s |

Latest-state visibility delay is the wait from an upsert's generation time until the first forwarded frame for the same `(owner, digest)` whose coverage is at least that upsert's coverage. All 384 offered states became visible in the merge-on log (`not_visible = 0`). P50 visibility is 13.01 s.

P95 delay of the frames that were actually forwarded is 31.97 s. That survivor delay is smaller than the latest-state delay and is not the merge comparison.

The accounting identity is the forwarded-count result: LatestOnly removed only superseded frames, and the cell drained. Merge off on the same arrivals would forward 384. Signaling reduction is `1 - 310/384 = 0.193`, and every removed frame is a duplicate.

The work-conserving HTB replay at the phase rates does not reproduce the log (21 versus 74 supersessions, 5.09 s versus 41.42 s). A single slower constant rate can match one of those two numbers and not both. Those replay rows are stored as `MODEL_REJECTED`. They are not a visibility effect.

### A-high, controlled compression

Source trace: `analysis/formal4t4/raw/traces/original_compatible_trace_rep0.csv` (120 requests). Each lineage is forced onto `lineage_id % 4`, and arrivals are spaced at one quarter of the 1754 bit/s frame time. This is a compressed clock. It is not a measured vLLM extension frequency, and it is not the missing baseline gateway log.

| Arm | Forwarded | Extension | Anomalous coverage drop | P95 visibility |
| --- | ---: | ---: | ---: | ---: |
| Merge on | 44 | 56 | 20 | 11.50 s |
| Merge off | 120 | 0 | 0 | 35.22 s |

The 20 anomalous joins are step wrap (`step % 3` returns coverage to the base). They are not extensions. The visibility gap is a property of this compressed clock, including those wraps. It is not a formal delay result.

## What this does and does not support

The reconstructable gateway logs separate extension from duplicate, and the separation is one-sided: extension opportunities are absent. The isolated signaling reduction is real for same-coverage repeats that are still pending. A visibility-delay improvement is not established for that observed cell.

Fig. 6(a) should be read as general pending-update supersession, not as extension-delay improvement. Details and the pairing table are in `F6_A_MERGE_PAIRING.csv` and `a_supersession_classification.csv`.
