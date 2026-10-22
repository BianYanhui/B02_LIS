# Fig. 7(b) fixed-K sensitivity

Source: `formal_A_5d2d300/ksweep_ov30`, commit `5d2d300`, clean tree. No cell was re-run. The audit is `EXISTING_DATA_AUDIT.md`.

## Setup

Static KVSelect, `global_topk` in {4, 8, 16, 32, 64}. Merge on, invalidation priority on, Krep = 2, Adaptive off, utility off. Workload ov30 at ρ = 2 on a fixed 10 Mbit HTB link (drain denominator 9407.55 frames/s, 104-byte frames). Useful pool 32 with mix `1024:1,2048:1,4096:1`. Background mix `256:70,1024:10,2048:10,4096:10`, so 30% of noise frames meet or exceed the 1024-token useful floor. Seeds 0–2, 133 scored requests per cell, same request identity across K. Client TTFT is the measured end-to-end value from these cells. It is not a model.

Routing FN is the existing loose definition: some worker holds at least 512 tokens of the digest, and the dispatcher view of that digest is below 512.

## Checks

| Check | Result |
| --- | --- |
| Fixed K actually configured | `StaticTopK{k}` sets `global_topk = k`. Peak FIFO depth is 5, 9, 17–18, 33–34, and 65–66. |
| Adaptive shrink or utility | `congested_fraction = 0`, `relay_drop_low_utility = 0` on every Static cell. |
| Same inputs | Scored request id, slot, and coverage match across all five K and across BoundedSemantic. |
| Same link budget | `fixed_link_bit_s = 10000000`, same capacity window, measured ρ = 2.000. |
| Failures | `failed_requests = 0`. Ledger gap 0 on every Static cell. |
| Mechanism split | On Static cells, superseded = 0, replica_cap = 0, queue_drop = 0, rate_limit = 0. The only drop counter that moves is `global_topk`. |

Pending distinct-lineage mean and P95 were not logged. They stay NOT_MEASURED. The cap is still binding: every K, including 64, records 4.4e6–8.3e6 top-K admission drops.

## Routing FN

Counts are false negatives out of 133. The mean is over three seeds. Bars in the figure are the seed minimum and maximum.

| K | Seed 0 | Seed 1 | Seed 2 | Mean | Min–max |
| ---: | ---: | ---: | ---: | ---: | --- |
| 4 | 1 (0.75%) | 2 (1.50%) | 7 (5.26%) | 2.51% | 0.75–5.26 |
| 8 | 2 (1.50%) | 0 | 1 (0.75%) | 0.75% | 0–1.50 |
| 16 | 3 (2.26%) | 6 (4.51%) | 3 (2.26%) | 3.01% | 2.26–4.51 |
| 32 | 2 (1.50%) | 1 (0.75%) | 2 (1.50%) | 1.25% | 0.75–1.50 |
| 64 | 0 | 6 (4.51%) | 0 | 1.50% | 0–4.51 |

Totals across 399 requests: 10, 3, 12, 5, 6. The sequence 2.51, 0.75, 3.01, 1.25, 1.50 is not monotone. A larger pending set does not produce a lower FN.

K = 8 is below K = 16 on every seed (2 vs 3, 0 vs 6, 1 vs 3). K = 32 is also below K = 16 on every seed (2 vs 3, 1 vs 6, 2 vs 3). K = 8 against K = 32 and K = 64 changes sign across seeds. Three seeds and single-digit counts are not a significance test.

## Signaling cost and the other counters

Forwarded metadata is `relay_forwarded * 104 / 133` scored requests. It counts every frame the gateway wrote, almost all of it background.

| K | MB/request mean | Forwarded frames/s | Top-K drops | Foreground undelivered / 143 | TTFT mean (min–max) ms | Reused tokens |
| ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 4 | 2.33 | 4971 | 8.31e6 | 14.3 | 1743 (1463–2050) | 1433 |
| 8 | 3.70 | 7877 | 6.57e6 | 4.7 | 1685 (1385–1951) | 1453 |
| 16 | 4.76 | 10148 | 5.20e6 | 17.7 | 1729 (1422–2064) | 1440 |
| 32 | 4.76 | 10140 | 5.21e6 | 7.0 | 1815 (1410–2124) | 1435 |
| 64 | 5.00 | 10641 | 4.91e6 | 3.7 | 1763 (1386–2042) | 1446 |

K = 16 and K = 32 forward the same number of bytes. The 10 Mbit link is already full there: the rate sits on the previously measured FullSync plateau, whose spread is 9063–10195 frames/s. K = 64’s mean is slightly higher, and its seed range (9992–11546) overlaps that plateau. K = 4 and K = 8 are below the plateau, so the pending cap, not the link, limits how much metadata leaves.

Reusable tokens stay near 1430–1450. TTFT seed ranges overlap at every K. Neither curve follows the FN ranking.

Merge and replica suppression contribute 0 drops. The traffic reduction relative to the offer (about 18815 noise frames/s) is top-K admission plus the link drain. Those two are different: admission chooses which digests may enter, and the link decides how many admitted frames leave.

## Why K = 8 can beat K = 16 here

K = 16 forwards about 2300 frames/s more than K = 8 and leaves more foreground updates undelivered (13, 20, 20 versus 7, 4, 3). The extra forwarded frames are noise. Both K values still drop millions of updates, so both caps are active. The consistent sign is compatible with queue competition: a larger admitted set fills the link with noise and the useful update misses the decision. It is not an event-level join from a dropped digest to the request that later missed it. That join was not logged. Random fluctuation is unlikely to put the same sign on all three seeds, and it is also not a license to call the gap significant. The absolute gap is nine requests in 399.

K = 32 keeps the byte rate of K = 16 and has fewer FN and fewer undelivered foreground updates on every seed. That does not show up as lower TTFT. Mean TTFT is higher, and the seed ranges overlap.

## Coverage priority versus the FIFO cap

`delta` is Static FN minus BoundedSemantic FN, in percentage points. Negative means the coverage cap has fewer false negatives. BoundedSemantic’s number is a FIFO length, not a distinct-lineage cap. K = 8 and 32 were not run for BoundedSemantic.

| K | Static mean | BoundedSemantic mean | Δ mean (min–max) |
| ---: | ---: | ---: | --- |
| 4 | 2.51% | 2.26% | +0.25 (−1.50 to +3.01) |
| 16 | 3.01% | 9.52% | −6.52 (−8.27 to −3.76) |
| 64 | 1.50% | 20.80% | −19.30 (−22.56 to −15.79) |

At K = 4 the two rules are not separated. At K = 16 and K = 64 the coverage cap is lower on every seed, and the gap widens as the FIFO is allowed to hold more low-coverage noise. That is the Fig. 7(a) contrast, measured at three widths of the same numeric knob. It is not evidence that raising Static K itself lowers FN.

## Answers

1. FN falls from K = 4 to 8, rises at 16, then falls at 32 and stays near there at 64. Not monotone.
2. K = 8 and K = 32 are each below K = 16 on all three seeds. K = 8 against K = 32 is not stable. The counts are single-digit.
3. The distinct-lineage cap binds at all five K, including 32 and 64, because top-K drops stay in the millions. The link binds from K = 16 upward: more pending slots do not increase delivered bytes.
4. The K = 8 versus K = 16 FN gap lines up with more noise forwarded and more foreground updates left undelivered at K = 16. That supports queue competition over a coin flip. It does not identify the evicted digest. It does not move mean TTFT outside the seed overlap.
5. K = 16 is a reasonable default because it is the smallest measured K that fills the 10 Mbit metadata budget. It is not the FN minimum on this workload. K = 8 has fewer false negatives and forwards less. K = 32 matches K = 16’s byte rate and has fewer false negatives, without a TTFT gain. No one of those facts selects a universal K.

## Paper paragraph

At ρ = 2 on a 10 Mbit link with 30% long-coverage background, routing FN is not monotone in K. Three paired seeds average 2.51%, 0.75%, 3.01%, 1.25%, and 1.50% at K = 4, 8, 16, 32, and 64. K = 8 is lowest on every seed; each cell has 0–7 events among 133 requests. Forwarded metadata rises from 2.33 MB/request at K = 4 to 4.76 MB at K = 16 and is flat at K = 32. K = 16 is the signaling knee, not an FN optimum.

## Caption

Fixed-K sensitivity of Static KVSelect on ov30 (ρ = 2, 10 Mbit, seeds 0–2). Points are seed means. Bars span the seed minimum and maximum, not a confidence interval. The diamond marks the default K = 16. Routing FN is the fraction of scored requests whose prefix is resident on some worker and missing from the dispatcher view.

## What this evidence does not support

- That routing FN decreases as K increases, or that a larger pending set is always more visible.
- That K = 16 minimizes FN, TTFT, or reused tokens.
- That the K = 8 advantage, or the K = 32 advantage over K = 16, is statistically significant, or that it holds on the base pool-16 workload where FN was already 0.
- That the FN gaps explain the TTFT differences. The TTFT ranges overlap.
- A time-averaged or P95 pending-lineage occupancy. Only the FIFO peak and the drop counter were recorded.
- A BoundedSemantic comparison at K = 8 or K = 32.
- Any claim that Coverage Dominance changed these cells. Replica rejects were 0, and every digest has a single coverage.
