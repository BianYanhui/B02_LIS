# F6-F1 paper readiness for Fig. 6(a) and Fig. 6(c)

Fig. 6(b) and Fig. 6(d) were not run. This note does not reopen them.

## Fig. 6(a)

The five Dynamic LatestOnly logs are consistent, conserved, and classified. Same-coverage pending supersession removes a mean of 79.8 of 384 offered updates (20.78%, sample SD 2.75 percentage points). True extension supersessions are 0. Merge-OFF is an accounting identity, not a live arm.

The subplot can be drawn only as a compact offered-versus-forwarded bar for pending same-coverage supersession, with the five reductions shown. The candidate is `fig6a_candidate.pdf`.

It does not have the depth of a delay-versus-frequency result. Using it to claim extension aggregation, queueing-delay reduction, or an online A/B test would overstate the logs. If the paper needs four deep panels, this count should stay a narrow caption or be left out. More seeds on the same workload will not create extension supersessions.

Claims that can be written:

- On the recorded dynamic LatestOnly logs, pending duplicate advertisements for the same worker and digest are superseded.
- The removed fraction is about 21% of offered updates, with the per-repetition range 18.2–24.5%.

Claims that cannot be written:

- Coverage-increasing extension aggregation.
- A measured Merge-OFF run (`OBSERVED_MERGE_OFF`).
- Lower queueing delay or better latest-state visibility. The rejected HTB replay stays rejected.
- The old eight-cell means 37.78 / 30.95 / 15.18 as extension merges.

## Fig. 6(c)

All 12 live cells and 12 paired replays pass the accounting checks. The reduction figure can be drawn. The candidate is `fig6c_candidate_reduction.pdf`. Offered volume must appear with the reduction, because offered itself rises from 48 to 144. The forwarded-count bars are a supplement, not a cross-overlap comparison of benefit.

Claims that can be written:

- Under this fixed workload, a frozen `Krep=2` replay of the live FullSync stream removes a mean 6.3%, 31.7%, 39.3%, and 42.8% of advertisements at 0%, 25%, 50%, and 75% phase-0 overlap.
- The 0% cell is not uniformly zero: one seed removes nothing, and another removes 14.6%.
- The cap hides owners. Hidden owners are not a fixed pair of worker ids, and advertisement drops exceed unique hidden owner–prefix pairs.

Claims that cannot be written:

- That `Krep=2` was enabled on the live four-GPU service.
- TTFT, routing regret, or pressure-feedback effects.
- That reduction is strictly monotone for every future seed. These three seeds increased at each step; the last step is only 3.5 percentage points, and the data were not edited.
- Measured TCP byte savings. Only the 64-byte payload convention is reliable.
- A gateway-arrival timestamp. The order is the FullSync FIFO release order, corroborated by zero relay drops. The quiet relay did not emit per-update arrival times.

## Evidence labels

| Quantity | Label |
| --- | --- |
| Fig. 6(a) offered, forwarded, superseded, and the duplicate classification | OBSERVED gateway logs |
| Fig. 6(a) Merge-OFF forwarded count | ACCOUNTING_IDENTITY |
| Fig. 6(c) offered stream and its order | LIVE_VLLM_OFFERED_TRACE, captured at the FullSync FIFO dispatcher |
| Fig. 6(c) no-suppression and `Krep=2` forwarded counts | PAIRED_FROZEN_RELAY_REPLAY |
| Gateway per-update arrival timestamp | NOT_MEASURED |
| TTFT, routing regret, Fig. 6(b), Fig. 6(d) | NOT_MEASURED in this round |

No additional cell is required before a caption decision. A new live `Krep=2` serving run would be a different experiment from the paired replay this figure reports.
