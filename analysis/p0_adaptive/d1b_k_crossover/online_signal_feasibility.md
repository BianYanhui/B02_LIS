# Online signal feasibility

No online rule was implemented. The question is whether the gateway already sees a state that would have selected K=16 on base and K=8 on ov30.

## Signals that were recorded

| Signal | Base, FN = 0 for both K | ov30, where K=8 has fewer FN |
| --- | --- | --- |
| Pending peak | On the cap: 8–9 or 17 | On the cap: 9 or 17–18 |
| Top-K drops | 6.55e6 at K=8, 5.29e6 at K=16 | 6.57e6 at K=8, 5.20e6 at K=16 |
| Forwarded rate | ~7900/s or ~9990/s | ~7880/s or ~10150/s |
| Foreground lag P95 | ~3 ms or ~12 ms | ~3 ms or ~12 ms |
| Gateway CPU | 0.57 or 0.63 | 0.58 or 0.63 |

The two regimes look the same on every control signal. The difference is the workload, which the gateway does not receive as a label. A rule that shrinks K when the queue is full, or when drops are high, or when lag is a few milliseconds, would fire on base, where shrinking does not change TTFT or reuse. A rule that grows K when the queue has spare room would not fire here: both caps are full.

## Signals that were not recorded

Dropped-candidate coverage, the coverage histogram of ranks past K, recent prefix-hit rate, and per-update delivery of a digest that a later request needed are `NOT_MEASURED`. P0-D1 already found that drop count and queue length stay high when FN is 0. Adding them together does not separate these two regimes, because both are high in both regimes.

Recent routing FN would differ on ov30 and not on base. The gateway does not have that outcome, and it must not use the future request. No delayed-feedback design was tested. A combination that might work later is `NOT_SUPPORTED` until it is measured.

## Offline oracle

Label the regime by the workload name and pick K=16 for base and K=8 for ov30. Mark that `OFFLINE_ORACLE_UPPER_BOUND`. On the measured user metrics it ties fixed K=8, because K=16 does not improve base TTFT or reuse. It is not an online controller, and it does not beat the fixed smaller K.

Detection delay, misclassification rate, and switch chatter are `NOT_MEASURED`. There is no online classifier to score.
