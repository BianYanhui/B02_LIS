# P0-D1 adaptive feasibility

Primary case: `CAPACITY_TRADEOFF_WITHOUT_NET_GAIN`.

The comparison is the existing ov30 ultrahigh sweep, three seeds, K in {4, 8, 16, 32, 64}, 10 Mbit, commit `5d2d300`. K=24 was not run. No parameter was changed to favor a larger K. Adaptive was not implemented and Algorithm 1 was not edited.

A second, separate conclusion is `ADAPTIVE_CONTROL_NOT_YET_FEASIBLE`: the online signals that the gateway already has do not mark the cells where a routing FN appears.

## Answers

**Q1. Does K=16 bind?**

Yes, as a pending-set cap, on every formal StaticTopK16 cell that was inspected. Peak queue is 16–19. On the base workload that is the useful pool of 16 plus a tombstone or two. Drop counts go from a few hundred at ρ=0.5 to about 5e6 at ρ=2.

It does not bind as a routing-FN cap on that base workload. FN rate is 0 from ρ=0.5 through ρ=2, including the three main ultrahigh seeds. The drops there are the noise that cannot outrank a 4096-token useful prefix.

It does bind against useful state on the ov30 sweep, where the useful pool is 32 and noise coverage overlaps the useful coverages. K=16 leaves 13–20 of 143 foreground updates undelivered and produces 3, 6, and 3 loose false negatives in the three seeds.

**Q2. How many dropped updates change a later route?**

`NOT_MEASURED` at update level. There is no gateway event log in the sweep directory, so a dropped frame cannot be joined to a later request.

At request level, K=16 has 12 loose false negatives across 399 scored requests (133 × 3). That is the count. It is not a count of evicted updates.

**Q3. Do K=24 or K=32 repair them?**

K=24 is `NOT_MEASURED`.

K=32, same request ids: it removes 3, 6, and 3 of the K=16 false negatives and creates 2, 1, and 2 new ones. Net FN rate 0.030 → 0.013. Mean paired TTFT change, K=32 minus K=16, is +210 ms, −12 ms, and +60 ms. Mean reused tokens do not rise (1440 → 1435). K=64 removes the K=16 false negatives in seeds 0 and 2, and on seed 1 it removes 5 and creates 5.

K=8, on the same cells, ends at 2, 0, and 1 false negatives. The paired TTFT changes are +18 ms, −38 ms, and −113 ms. The lowest measured FN in this sweep is K=8, not a K above 16.

**Q4. What does a larger K cost?**

From 16 to 32, forwarded rate does not rise (10148 / s → 10140 / s). The 10 Mbit link is already full. Peak queue goes from 18 to 33. Foreground lag P95 goes from 12.3 ms to 13.3 ms. Gateway CPU goes from 0.634 to 0.694. From 16 to 64, CPU goes to 0.837 and lag P95 to 15.4 ms, while the forward rate only reaches 10641 / s.

Configured K, occupied depth, and forwarded frames are three different numbers. Here the depth tracks K, and the forwarded rate stops rising once the link is full.

**Q5. Does the best K change across regimes?**

On the base regime, only K=16 was run, and its FN rate is 0. There is no measured gain available for a larger K to claim.

On the ov30 ultrahigh regime, which is also the capacity-limited regime (the link is saturated), the best measured FN is K=8. A larger K is not the winner. A cross-regime change of the form "16 at low demand, 24 under a burst" is `NOT_SUPPORTED`. The sweep that varies K exists at only one workload point.

**Q6. Which online signals would see that change?**

Pending occupancy and Top-K drop count are high in cells whose FN rate is 0. At ρ=0.5 the peak queue is already 16. Those two signals do not separate noise rejection from a useful-prefix miss.

Routing FN and a useful-update ratio need the request outcome. The gateway does not have worker KV residency as an input, and a future request must not be an input. No signal in the current relay distinguishes the ov30 FN cells from the base cells except by already knowing the workload.

**Q7. Expansion, or a larger static K?**

Neither, on this evidence. Raising the static default above 16 does not improve TTFT or reused tokens on the only sweep, and K=8 has fewer false negatives there. Building a controller that raises K from 16 toward 24 or 32 is not supported.

**Q8. Start P0-D2?**

No.

## Case

`CAPACITY_TRADEOFF_WITHOUT_NET_GAIN`. On the workload where K=16 actually drops useful updates, K=32 lowers the FN rate by about 0.018 and does not lower TTFT. The link was already full, so the extra slots do not buy extra delivery. They do buy a deeper queue and more gateway CPU.

`ADAPTIVE_CONTROL_NOT_YET_FEASIBLE` is the control conclusion, not a second performance result. Occupancy and drop rate cannot be the expansion trigger, because they are on when the route is already fine.

This is not `STRONG_ADAPTIVE_OPPORTUNITY`. TTFT and reuse do not move in favor of the larger K, and the other regime was not hurt by K=16's routing FN in the first place.

This is not `LARGER_STATIC_K_SUFFICIENT`. K=32 is not better on TTFT, and K=8 is better on FN inside the same sweep.

This is not `NO_EXPANSION_BENEFIT` in the strict sense that K=16 never drops anything that matters. On ov30 it does coincide with a handful of false negatives. The net of raising K is still not a gain.

This is not only `CONDITIONAL_BENEFIT_ONLY`. The binding itself is in the paper's own 10 Mbit matrix, not in a burst invented for this round. The absence of a net gain is what stops the expansion.
