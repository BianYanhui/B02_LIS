# P0-D3 final verdict

Primary case: `NO_INDEPENDENT_UTILITY_GAIN`.

The only identified utility admission effect is a pressure-gated reject of 1024-token updates. That decision does not use freshness. No measured user-side increment of this filter over StaticTopK16 exists. Dynamic K remains without a measured user-side win from P0-D1 and P0-D1b. Adaptive should not stay in the main technical claims of the ICC submission.

Recommendation: `REMOVE_ADAPTIVE_FROM_MAIN_DESIGN`.

This round does not delete the code, does not edit Algorithm 1, and does not edit the figures. "Remove" means stop citing it as a measured contributor. The next paper pass has to make that edit.

## Answers

**Q1.** On formal4t4 reuse, yes, as a pre-enqueue reject: 942 upserts, all 1024 tokens, ages 0.1–1.4 ms. On formal4t4 original, the pre-enqueue reject count is 0. On fixed 10 Mbit Run B, the utility counter and the congestion latch are both 0. The cell column `relay_drop_low_utility` at commit `1dc24ec` also contains shrink deletions and is not this count.

**Q2.** Pre-enqueue: only 1024. Post-enqueue events with the same reason also include 2048 and 2560. Those are not a clean utility class. See Q3.

**Q3.** No identified case. Pre-enqueue rejects of 2048 and 4096 are 0. A freshness failure for 2048 needs `age + EWMA >= 20.79 s`. The post-enqueue `low_utility` events never show a queue age that large, and EWMA is not in the log, so they stay `NOT_SEPARABLE` rather than a freshness count. The sub-50 ms post-enqueue deletions had already passed the utility test and match trim-to-K/4.

**Q4.** Not shown. formal4t4 has no StaticTopK16 arm. The 161 ± 85 ms figure is 20 reuse pairs against StaticSemantic (mean 161.0 ms, sample SD 193.3). That baseline has top-K off. Run B does pair Adaptive with StaticTopK16, and there utility rejected nothing, so the pair does not measure a filter.

**Q5.** No end-to-end increment was run. At admission, the observed reject set is exactly the set `coverage <= 1024` once the gate is open. A freshness term is not required for any of the 942 decisions. `PressureCoverageThreshold16` versus `PressureUtilityOnly16` is `NOT_MEASURED` as a user-side pair and is not needed to see that the formula collapsed to a threshold on this trace.

**Q6.** `STATIC_K8_NOT_STRICTLY_PAIRED`. formal4t4 has no K=8. The 10 Mbit K=8 result in P0-D1b is a different link and a different workload, and on that link utility did not run. There is no pair in which utility beats a fixed K=8. `SIMPLER_STATIC_POLICY_COMPETITIVE` applies to the tested static-K workloads, not as a claim that K=8 was compared with a live utility gate.

**Q7.** Not shown past the bundle. The 942 rejects are not joined to a route, a reuse count, or a TTFT. ρ=0.5 of the bundled reuse contrast is about zero. The original-compatible bundled contrast is not positive.

**Q8.** Figure 7(b), as specified by the sensitivity supplement, is full Adaptive against StaticSemantic and RateFIFO at one prefix length. The theta panel is the same full policy against StaticSemantic. Both speak to the bundle. Neither isolates freshness.

**Q9.** Yes. The sentence that attributes 161 ± 85 ms to freshness-aware admission should be changed before submission. The contrast is the whole Adaptive policy against StaticSemantic. This round does not edit the paper. P0-B1 already drafted a replacement that keeps the 161 ms number and names the bundle.

**Q10.** `REMOVE_ADAPTIVE_FROM_MAIN_DESIGN`. Not `KEEP_FULL_ADAPTIVE`: shrink and the freshness term have no isolated user win. Not `KEEP_UTILITY_ONLY`: the utility test did not use freshness, and its user increment is unmeasured. Not `SIMPLIFY_TO_COVERAGE_THRESHOLD`: the 1024 gate matches the events, and promoting it would invent a contribution that was not compared with StaticTopK16. Not `INCONCLUSIVE` as a reason to run another matrix. The missing A/B/C TTFT pair is real, and it is not a license to search for a setting where the filter wins. The events already say the filter is a 1024 threshold. That is enough to stop claiming a freshness mechanism.

## What was not run

Smoke and the four-policy pair were not started. Pressure on the 10 Mbit path is already a negative control. Pressure on formal4t4 reuse already produced the 942 classified rejects. Building B and C inside the relay would change admission in order to chase a user-side delta. The stop rule for this round is to accept the missing increment.
