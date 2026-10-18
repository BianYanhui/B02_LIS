# Next iteration

Do not start this. The recommendation is a held-load matrix on the same prefix, not an overlap sweep and not Fig. 6(b).

## Verdict

**Case A.** On engine-confirmed snapshots, model regret is 2.0 ms when a hidden owner is strictly lighter and 0 when a visible owner is at least as light. Seed 2 reproduced the forward result. The reverse order produced no positive regret. The number is not a TTFT.

## Do not run next

- Replica overlap × load matrix. Coverage did not differ across owners.
- Fig. 6(b), a TTFT suite, a paper figure, or a LaTeX edit.
- More seeds of the same ramp. Seed 2 already reproduced the forward ramp. Another seed would still be the same passing imbalance.

## If one experiment is approved

Hold the load instead of sampling a ramp.

- Orders: `[0,1,2,3]` and `[3,2,1,0]`. Admission stays 0, 1, 2, 3.
- Targets: DeltaLoad +1 and DeltaLoad -1, each kept until `num_requests_running` equals the harness count. Drop the phase if it never confirms. Do not retune the cap.
- Seeds: 1 and 2.
- Views: full and `Krep=2`, same snapshot, frozen `choose`.
- Count: 8 phases, 1 GPU boot, 4 T4s. Startup about 2 minutes. Measurement about 3 minutes if each hold stays near 20 s. Stop a phase whose warmup exceeds 120 s.
- Record model regret and, separately, the TTFT of the one request that was actually executed. Do not call that TTFT the other view’s outcome.

The question that matrix answers is whether a held gap of one request still costs 2.0 ms of model time, and whether the reverse hold still costs 0. It does not answer overlap.
