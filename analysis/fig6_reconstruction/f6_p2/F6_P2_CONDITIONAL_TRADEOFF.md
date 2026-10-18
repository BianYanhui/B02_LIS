# F6-P2 conditional trade-off

## Cost and the extra indicator

`T_i = load_i * 2 ms + max(0, 1083 - cached_i) / 50`.

Cached tokens were 1056 on every owner, so the prefill term is 0.54 ms everywhere and cancels. `Regret = T(cap selected) - T(full selected)` from frozen `Dispatcher.choose`. Label: **MODEL-DERIVED**.

`DeltaLoad = min(load of workers 0 and 1) - min(load of workers 2 and 3)`.

Those sets are the owners the cap keeps and drops. A positive DeltaLoad means a hidden owner is strictly lighter. It is not a sufficient condition by itself: `choose` is not argmin of `T`. In this dataset the two happened to agree. Full view’s selected worker was inside the model argmin set on every engine-confirmed row.

No counterfactual request was executed. `actual_ttft_ms` is empty.

## Engine-confirmed comparison

One cell is one trajectory. Snapshots inside a cell are not independent seeds. Means use engine-confirmed ramp samples only. Idle samples are a separate row. There is no significance test.

| Condition | Cells | Snapshots | Engine-confirmed | Unique confirmed loads | Mean model regret (ms) | Positive rate | Mean DeltaLoad |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| F6-P1 Forward | 1 | 8 | 3 | 3 | 1.333 | 0.667 | 0.667 |
| F6-P2 Reverse | 1 | 8 | 4 | 4 | 0 | 0 | -0.5 |
| F6-P2 Interleaved | 1 | 8 | 8 | 8 | 0.25 | 0.125 | 0 |
| F6-P2 Forward, seed 2 | 1 | 8 | 8 | 8 | 1.0 | 0.5 | 0.5 |
| F6-P1 idle | 1 | 1 | 1 | 1 | 0 | 0 | 0 |
| F6-P2 idle | 3 | 3 | 3 | 1 | 0 | 0 | 0 |

Advertisements were 2 forwarded and 2 dropped on every confirmed row. Selected-worker change rate equals the positive-regret rate: the cap changes the choice only when it also raises `T` by 2.0 ms.

Among all engine-confirmed points from P1 and P2, regret is 2.0 ms exactly when DeltaLoad is 1, and 0 when DeltaLoad is 0 or -1. There is no confirmed point that breaks that split. The positive points are:

| Cell | Load | Full | Cap | Best owner hidden |
| --- | --- | ---: | ---: | --- |
| P1 B1, and C sample 1 | 1,1,0,0 | 2 | 1 | yes |
| P1 B2, B sample 2, C sample 2 | 1,1,1,0 | 3 | 1 | yes |
| C sample 5 | 2,2,1,1 | 2 | 1 | yes |
| C sample 6 | 2,2,2,1 | 3 | 1 | yes |

The internal scatter is `deltaload_regret_diagnostic.svg`. It is not Fig. 6.

## Answers

**Q1. Did the forward condition reproduce a positive regret?**

Yes. Seed 2, engine-confirmed, reproduced `[1,1,0,0]` and `[1,1,1,0]` at 2.0 ms, and added confirmed `[2,2,1,1]` and `[2,2,2,1]` at the same 2.0 ms.

**Q2. Did the reverse load reduce the regret?**

Yes. The four engine-confirmed reverse samples have regret 0. Two of them have DeltaLoad -1, so a visible owner is the lighter one. The cap still drops two advertisements.

**Q3. Does DeltaLoad line up with regret?**

In these confirmed snapshots, yes: DeltaLoad 1 lines up with 2.0 ms, and DeltaLoad 0 or -1 lines up with 0. That is what `choose` returned. It is not a proof that DeltaLoad > 0 will always produce regret if coverage differs or the 0.5 ms guard fails.

**Q4. Does `Krep=2` keep reducing advertisements?**

Yes. Every snapshot, confirmed or not, forwarded 2 and dropped 2. The reduction does not depend on the load.

**Q5. Is that enough to claim the efficiency–flexibility trade-off?**

Enough for a conditional model-cost claim on this prefix: the cap always halves the replica advertisements, and the model penalty appears when a hidden owner is strictly lighter. Not enough for a measured serving claim. The penalty is one formal queue weight, not a TTFT, and the live gateway path was not in this cell.

**Q6. What real-system evidence is still missing?**

A TTFT for the worker each view would have run. A load gap larger than one request. Any prefix that is not on all four workers. Engine block hashes. A reverse-cell second wave whose gauge matches the harness. More than one trajectory per order. These snapshots are one ramp each, not a sample of independent requests.

**Q7. Is a replica-overlap matrix worth running?**

No. Coverage was 1056 on every owner, so the prefill term never moved. An overlap sweep would change a term this cell did not exercise.

**Q8. If a formal follow-up is worth it, what is the small matrix?**

A load-advantage matrix, not an overlap matrix. Two orders, forward and reverse. Two held gaps, DeltaLoad +1 and -1, held until the running gauge matches, rather than only the passing ramp. Two seeds. Eight short phases on one boot of the four T4s. About 2 minutes to start and about 3 minutes of measurement if each phase stays near 20 s. Offline full view and `Krep=2` only. No Fig. 6(b), no TTFT suite, no overlap axis.
