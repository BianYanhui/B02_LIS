# Next iteration

F6-P3 is the last readiness round. Do not start the formal matrices until the user accepts this plan.

## Readiness

| Figure | Status | Can a formal run start? | Missing piece |
| --- | --- | --- | --- |
| Fig. 6(a) | `READY_WITH_LIMITATIONS` | Yes, for the forwarded-count comparison on the five existing dynamic logs | A validated clock, if the y-axis is going to be visibility delay. Extension frequency is the wrong x-axis |
| Fig. 6(c) | `READY_WITH_LIMITATIONS` | Yes | New seeds. The readiness seed `2026072640` stays a pilot. Plot offered counts or the reduction, not raw forwards alone |
| Fig. 6(d) | `READY-MODEL` | Yes, for model regret | Executed TTFT pairing. Do not design a TTFT matrix |
| Fig. 6(b) | Closed | No | Unchanged from F6-P0 |

## Order

1. Assemble Fig. 6(a) minimal from `a_supersession_classification.csv`. No GPU. Keep the claim on same-coverage supersession.
2. Run Fig. 6(c) minimal: seeds `2026101901`–`2026101903`, overlaps 0, 0.25, 0.5, 0.75, one FullSync recording each, then the offline Krep=2 replay. About 30 minutes, one boot.
3. Run Fig. 6(d) minimal on a fresh boot: seeds 3, 4, 5, DeltaLoad −1, 0, +1, admission order 0, 1, 2, 3. About 5 minutes of measurement. No TTFT pair.

Enhanced (c) and (d) wait on the minimal results. The Fig. 6(a) service-rate sweep waits on the rep-0 gate (within 10% of 74 supersessions and within 20% of 41.42 s). That gate has already failed for the phase-rate HTB model.

## Do not

- Promote F6-P3 smoke rows, calibration cells, or `MODEL_REJECTED` delays into the paper.
- Call the dynamic cell a 10 Mbit/s or production bottleneck.
- Treat gateway arrival order as worker 0 then 1.
- Merge to `main`.
- Open Fig. 6(b), a TTFT suite, pressure 2×2, Dynamo, a 64-GPU replay, or the paper LaTeX.
