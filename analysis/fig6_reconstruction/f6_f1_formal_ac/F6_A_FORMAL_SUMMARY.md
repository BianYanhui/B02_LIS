# F6-F1 Fig. 6(a) — Formal summary of five Dynamic LatestOnly logs

## Scope

This is a recount of existing gateway logs. No new seed and no GPU run. The claim that can be supported is narrow: when a repeated advertisement for the same `(worker, digest)` is still sitting in the gateway pending queue, LatestOnly supersession removes that unsent copy.

It is not a measurement of coverage-increasing extension aggregation. True extension supersessions are zero in every repetition.

## Sources

Five recorded Dynamic LatestOnly logs, repetitions 0–4:

`analysis/formal4t4/raw/dynamic/gateway_events_dynamic_LatestOnly_rep{0-4}_20260726.csv`

Metadata: `run_metadata_dynamic_LatestOnly_rep{0-4}_20260726.json`.

Shared recorded configuration: workload `reuse_intensive`, 320 requests, pool 64, overlap 0.25, rho `0.5`. Offered upserts are 384 in every repetition (64 preseed advertisements + 320 request advertisements).

Seeds: `2026072700`, `2026072701`, `2026072702`, `2026072703`, `2026072704`.

Classifier: `analysis/fig6_reconstruction/f6_p3/run_ac_readiness.py` `classify_log`, reused by `run_f6_a_formal.py`. A superseded row is joined to the next log row. Coverage increase is `EXTENSION`, equal coverage is `DUPLICATE`, a same-key tombstone is `TOMBSTONE`, and every other join is `OTHER` or `ANOMALOUS`.

Excluded:

- F6-P3 HTB replays marked `MODEL_REJECTED`.
- The formal baseline corpus (184 offered upserts). The published eight-cell means 37.78 / 30.95 / 15.18 stay `NOT_RECONSTRUCTABLE` and are not extension-merge counts.
- StaticSemantic and churn logs. Those bundle other mechanisms.

Parent commit at the recount: `7605cd5d7d3679e3c6a6aaf8ccc5f6ba13e1d513`.

## Conservation

For every repetition:

`forwarded + superseded + other drops + undrained = offered`

| Rep | Seed | Offered | Forwarded | Superseded | Extension | Duplicate | Tombstone | Other | Anomalous | Other drops | Undrained | Balance | Merge-off evidence | Reduction |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| 0 | 2026072700 | 384 | 310 | 74 | 0 | 74 | 0 | 0 | 0 | 0 | 0 | 384 | ACCOUNTING_IDENTITY | 0.192708 |
| 1 | 2026072701 | 384 | 314 | 70 | 0 | 70 | 0 | 0 | 0 | 0 | 0 | 384 | ACCOUNTING_IDENTITY | 0.182292 |
| 2 | 2026072702 | 384 | 311 | 73 | 0 | 73 | 0 | 0 | 0 | 0 | 0 | 384 | ACCOUNTING_IDENTITY | 0.190104 |
| 3 | 2026072703 | 384 | 290 | 94 | 0 | 94 | 0 | 0 | 0 | 0 | 0 | 384 | ACCOUNTING_IDENTITY | 0.244792 |
| 4 | 2026072704 | 384 | 296 | 88 | 0 | 88 | 0 | 0 | 0 | 0 | 0 | 384 | ACCOUNTING_IDENTITY | 0.229167 |

All five repetitions balance. The classifier count matches the superseded-row count. Nothing was forced into balance.

`Forwarded_MergeOff = Offered = 384` holds for all five because each log balances, has no other drop, and is fully drained. The label is `ACCOUNTING_IDENTITY`. It is not `OBSERVED_MERGE_OFF`: there is no separate live Merge-OFF arm in these logs.

`Reduction_r = 1 - Forwarded_r / Offered_r` equals the superseded fraction. The paired count difference is the superseded count.

## Aggregate

Sample standard deviation uses `n - 1`.

| Metric | Mean | Sample SD | Min | Max |
| --- | ---: | ---: | ---: | ---: |
| Offered | 384 | 0 | 384 | 384 |
| Forwarded | 304.2 | 10.545 | 290 | 314 |
| Superseded / paired difference | 79.8 | 10.545 | 70 | 94 |
| True extension | 0 | 0 | 0 | 0 |
| Reduction | 0.2078125 | 0.027461 | 0.182292 | 0.244792 |

Offered size is the same in every repetition, so the macro mean of the five reductions equals the pooled ratio: `399 / 1920 = 0.2078125`. Individual updates are not treated as repetitions.

Observed enqueue span is about 342–405 s, so the offered rate across the log is about 0.95–1.12 updates/s. That is the span of the recorded arrivals, not a configured link rate.

Pending duration of a superseded copy (time from its enqueue to the later duplicate) has p50 between 2.20 s and 3.61 s and p95 between 16.55 s and 23.39 s. This is how long the replaced copy had been waiting. It is not a merge-on versus merge-off visibility comparison. The F6-P3 phase-rate HTB replay remains `MODEL_REJECTED` and is not used.

## Publishability

1. All five logs support the same count result. Every repetition is 384 offered, fully drained, extension 0, tombstone 0, other 0, and the entire superseded set is same-coverage duplicates. The reduction is positive in all five, from 18.2% to 24.5%.
2. True extension supersession is still 0.
3. Same-coverage supersession removes 79.8 advertisements per repetition on average (sample SD 10.5), which is 20.78% of the 384 offered updates.
4. The Merge-OFF count baseline holds as an accounting identity for all five repetitions. It was not observed as a live Merge-OFF run.
5. Supported claim: on this recorded dynamic LatestOnly workload, pending same-coverage duplicates are removed before send, and the removed fraction is about one fifth of offered updates. Unsupported: extension frequency, queueing-delay reduction, or an online A/B performance test. Five positive reductions are five repetitions of one observed arm plus an identity, not a paired live experiment.
6. Relabeling the subplot as pending-update supersession matches the only mechanism present in the logs. That is a real correction of the old extension-aggregation reading. It does not add a delay or frequency result the old figure was drawn to show.
7. These logs do not support a research-depth subplot. They support a compact, fully conserved count with a narrow caption. They should not be stretched to fill a four-panel performance figure.

Candidate figure (not the paper figure): `fig6a_candidate.pdf` and `fig6a_candidate.png`.

Per-repetition rows: `f6_a_per_rep.csv`. Aggregate: `f6_a_summary.csv`. Machine-readable audit: `f6_a_audit.json`.
