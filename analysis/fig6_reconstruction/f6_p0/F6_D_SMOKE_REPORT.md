# F6-D smoke report

## Verdict

**PASS**, scoped to a CONTROLLED counterfactual.

On one shared snapshot, `Krep=2` forwards 2 replica advertisements instead of 4. When the four owners have the same load, model routing regret is 0. When the owner that the cap hides is the idle one, model routing regret is 16 ms. That 16 ms is derived from the formal cost weights. It is not a measured TTFT. How often the formal trace looks like the imbalanced snapshot is NOT_MEASURED. No GPU smoke was run.

## Why the historical rows are not enough, and why GPU was not started

`signaling_accounting.csv` already shows the cap firing. Across the 8 formal cells, StaticSemantic drops a mean of 25.15 duplicate holders per cell, Adaptive 17.68, and the policies with `dedup=0` drop 0. Tombstones sent are 0 in all of those cells, so the holder set is not later released by an invalidation.

The overlap preseed really does request each seed prefix from all four vLLM ports and then advertise instances 0, 1, 2, 3. So more than two owners are created by the harness. The drop counter does not store the hidden owner’s load or a later request’s candidate set. Regret cannot be recovered from those CSVs.

The cost model only needs the snapshot. GPU time would measure TTFT, which this round is not allowed to substitute for `T`. GPUs were left idle.

## Retention rule used

The smoke calls the frozen `_admit_replica` via `Relay.enqueue`.

- Arrival order is instance 0, then 1, then 2, then 3.
- `dedup=2` keeps 0 and 1. Instances 2 and 3 are dropped.
- `dedup=1` keeps 0.
- `dedup=0` keeps all four.
- Load and coverage are not inputs to that choice. All four truth coverages are 2048 tokens. Prompt length is 2048, so the prefill term is 0 for every owner that still holds the prefix.
- A dropped owner is not visible to `Dispatcher.index`. Nothing else in this smoke reinserts it.

## Cost

`T_i = load_i * 2 ms + max(0, 2048 - coverage_i) / 50`.

Weights are the formal defaults (`--queue-penalty-ms 2`, `--prefill-tokens-per-ms 50`). Routing regret is `T(selected) - T(best)` on the truth snapshot, using the worker that frozen `Dispatcher.choose` returns under the restricted view. Label: **DERIVED**.

`choose` itself is not a pure argmin of `T`. It affinity-overrides the least-loaded worker only when the coverage gain beats the 0.5 ms guard. That is the same function for every view.

## Result

Table: `analysis/fig6_reconstruction/f6_p0/smoke_d/replica_regret.csv`

Script: `analysis/fig6_reconstruction/f6_p0/smoke_b/offline_priority.py`

| Scenario | View | Ads forwarded | Ads dropped | Selected | Best owner hidden | Regret (ms) | Prefill term difference |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| D-A loads 2,2,2,2 | full | 4 | 0 | 0 | no | 0 | 0 |
| D-A | Krep=2 | 2 | 2 | 0 | no | 0 | 0 |
| D-A | Krep=1 | 1 | 3 | 0 | no | 0 | 0 |
| D-B loads 8,8,0,8 | full | 4 | 0 | 2 | no | 0 | 0 |
| D-B | Krep=2 | 2 | 2 | 1 | yes | 16 | 0 |
| D-B | Krep=1 | 1 | 3 | 0 | yes | 16 | 0 |

D-A: every owner has the same `T` (4 ms). Hiding two owners does not change the selected worker at `rr=0`, and it would not change `T` if it did. Two optimal owners are hidden, but an optimal owner remains visible, so `best_owner_hidden` stays 0.

D-B: the unique best owner is worker 2 (load 0, `T` = 0). The full view selects worker 2. The affinity bit is false there, because worker 2 is already the least-loaded native and the coverage increment is 0. `Krep=2` hides worker 2. `choose` then affinity-overrides onto worker 1 (load 8). Regret is `8 * 2 ms = 16`. Coverage of the selected worker is still 2048, so the reusable-coverage difference is 0. The entire regret is the load term.

Advertisement counts and regret are from the same enqueue snapshot.

## Case check

D-PASS: advertisements fall, and under one load condition the model regret is not zero. This smoke matches that case, with the load marked CONTROLLED.

D-ZERO describes D-A only. It does not describe D-B.

D-FAIL would mean there is no owner-choice difference, or the owner set cannot be recovered. The frozen cap does drop the third and fourth instance, and the full owner set is the input to the counterfactual. Not D-FAIL.

## What remains NOT_MEASURED

- The fraction of formal requests for which the hidden owner was the cheapest.
- vLLM cached tokens on the suppressed owners at decision time. The preseed sent them the prompt. Later residency was not rechecked.
- Any TTFT difference between full view and `Krep=2`.
