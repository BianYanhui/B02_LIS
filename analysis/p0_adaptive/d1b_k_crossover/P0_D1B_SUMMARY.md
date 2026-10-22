# P0-D1b summary

Primary case: `STATIC_K8_SUFFICIENT`.

K=8 is not worse than K=16 on the user-facing metrics in either measured regime. There is no regime in which K=16 wins TTFT or reused tokens. Adaptive Top-K is not supported over a fixed K. This round does not change the default and does not start P0-D2.

n=3 everywhere. The bootstrap intervals below resample those three deltas. They are not a population significance test.

## Answers

**Q1. Is the ov30 advantage of K=8 consistent across seeds?**

Routing FN, yes. K=8 minus K=16 is −0.0075, −0.0451, −0.0150. All three seeds are lower. Mean −0.0226. The resampled interval stays negative only because every observed delta is negative.

Mean TTFT, no. The deltas are +17.9, −37.6, and −112.9 ms. Two seeds favor K=8. Seed 0 favors K=16 by 18 ms. The mean −44 ms is seed 2. Reused tokens have the same split: seed 0 is 7 tokens worse for K=8, seeds 1 and 2 are higher.

**Q2. Is K=4 better than K=8?**

No. On ov30, K=4 minus K=8 has higher FN on seeds 1 and 2 (+0.015, +0.045) and a lower FN only on seed 0 (−0.0075). P95 TTFT is worse on all three seeds. Foreground undelivered is worse on all three ( +6, +7, +16 ). K=8 is not being treated as a searched optimum. K=4 simply does not beat it.

**Q3. On base, which K has better TTFT and reuse?**

Neither. The same-session pair, seeds 0–2, ultrahigh, pool 16, noise coverage 256, 10 Mbit, commit `198a5b6`:

| Seed | TTFT K=8 | TTFT K=16 | Reused K=8 | Reused K=16 | FN |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 628.2 | 627.8 | 3293.4 | 3293.6 | 0 / 0 |
| 1 | 609.5 | 588.0 | 3385.7 | 3385.7 | 0 / 0 |
| 2 | 668.9 | 678.1 | 3293.4 | 3293.4 | 0 / 0 |

K=8 minus K=16 mean TTFT is +0.4, +21.5, −9.1 ms. Mean of those is +4.3 ms. The resampled interval is −9.1 to +21.5 ms. Reuse differs on one request in seed 0 and on none in seeds 1 and 2. All 143 foreground updates were applied for both K. FP is 0.

The October base K=16 TTFT of 3321–3767 ms is a different session. Reuse there was 2952–3139, close to this run, and the TTFT level is not. Those rows are `HISTORICAL_NOT_SAME_SESSION` and are not the comparison.

**Q4. Is there a real performance crossover?**

No end-to-end crossover. K=16 does not win TTFT or reuse on base, and it does not win them consistently on ov30.

There is a routing-accuracy difference on ov30 only: K=8 has fewer false negatives, and on base both rates are already 0. That is not a crossover. It does not reverse when the workload changes.

**Q5. What would have caused a crossover?**

Not identified, because the crossover is not there. The ov30 FN gap appears where the useful pool is 32 and noise coverage overlaps the useful coverages. Base at the same ρ=2 and the same 10 Mbit link has FN 0 for both K. Pool and overlap change together with the noise mix, so the FN gap is not attributed to control pressure alone.

**Q6. Is there a direct mechanism for the FN change?**

No event-level chain. `CAUSAL_CHAIN_NOT_VERIFIED`. On ov30, K=8 leaves fewer foreground updates undelivered (7, 4, 3 versus 13, 20, 20) and its foreground lag P95 is about 3 ms versus 12 ms. On base the same lag gap exists and every foreground update is delivered either way, with no FN and no TTFT change. A lower forward rate does not by itself mean the useful update arrived earlier in a way that changes the request. H4, “fewer FN means lower TTFT”, is `NOT_SUPPORTED`: ov30 FN fell on all three seeds and TTFT did not.

**Q7. Can the gateway tell which K to use?**

No signal that was checked can. On base, peak queue sits on the cap (8–9 or 17) and Top-K drops are about 6.6e6 at K=8 and 5.3e6 at K=16, while FN is 0. Occupancy and drop count are high in the regime where the choice does not change the user result. Dropped-candidate coverage and a recent hit rate are `NOT_MEASURED`. No combination was tested, so none is claimed to work.

**Q8. Would Adaptive beat a fixed K?**

No. The only user-side difference is the ov30 FN count, and it favors the smaller fixed K. Base does not reward the larger one. An oracle that picked 16 on base and 8 on ov30 would match “always 8” on the user metrics, because 16 does not help base. That oracle is `OFFLINE_ORACLE_UPPER_BOUND` and it is not better than fixed K=8.

**Q9. Start P0-D2?**

No.

## What was run

Smoke, 60 s, StaticTopK8, seed 0: queue peak 8, top-k drops 649201, forwarded frames equal dispatcher frames, no failed requests. Then six formal cells, StaticTopK8 and StaticTopK16, seeds 0–2, 600 s, one process, exit 0. Same arrival 0.237, capacity denominator 9407.55, burst multiplier 1.5, invalidate every 8. The 10 Mbit path is the formal HTB ceiling. It is a controlled overload, not natural congestion.

## Case boundary

`CONTROL_COST_TRADEOFF_ONLY` describes the base regime by itself: the user metrics tie, and K=16 forwards about 2100 frames/s more and uses about 0.05 more of the gateway CPU. It is not the cross-regime verdict, because ov30 also moves the FN rate. That movement does not create a region where the larger K is the one to switch back to.
