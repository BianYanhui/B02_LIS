# Mechanism and causal audit

No A/B/C/D run was started. Policies B and C do not exist in the relay, and adding them would be a new admission rule. The historical events already say what the formula rejected.

## What was rejected before enqueue

formal4t4 Adaptive, commit `1dc24ec`, gateway events. One class per upsert.

Reuse-intensive, 20 cells, pre-enqueue `low_utility`:

| ρ | 1024 | 2048 | 4096 |
| ---: | ---: | ---: | ---: |
| 0.5 | 117 | 0 | 0 |
| 0.8 | 231 | 0 | 0 |
| 1.0 | 289 | 0 | 0 |
| 1.2 | 305 | 0 | 0 |

Total 942. Queue age at these events is 0.1–1.4 ms. The freshness factor is 1 at that age. The reject is `1024 - 1024 <= 0`. Class `LOW_COVERAGE_REJECT`.

Original-compatible, 20 cells: pre-enqueue utility rejects are 0 at every ρ. Those prefixes are 2048, 2560, and 3072. Class `FRESHNESS_DEPENDENT_REJECT` count is 0 in both workloads. That zero is `OBSERVED`.

## What the mixed counter still contains

After a frame has been enqueued, `1dc24ec` emits `low_utility` for two different actions: `trim_global_topk` while shrinking to K/4, and the dequeue recheck. The event reason is the same. EWMA is not stored.

Events suppressed within 50 ms of enqueue are `TOPK_SHRINK_MISLABELED`. Utility had already returned false, or the frame would not have been enqueued, and trim runs in that same call. On reuse that bucket is 61 frames, of which 59 are 2048 and 2 are 1024.

Events suppressed after 50 ms are `POST_ENQUEUE_NOT_SEPARABLE`. On reuse that is 197 frames (43 at 1024, 154 at 2048). On original it is 119 frames, all 2048 or 2560. Queue age in this bucket stays under 14.4 s. A 2048-token freshness reject needs 20.79 s of age plus EWMA. Age alone never reaches that. EWMA is `NOT_MEASURED`, so this bucket is not counted as a freshness reject and not counted as a pure shrink.

## Chain

Pressure is inferred, not read from a latch counter. formal4t4 did not record `congested_fraction`. Deletes happen only inside `congested()`, and forwarded waits of several seconds are in the P0-B1 tables. That is enough to say the gate was open on reuse and on original at ρ ≥ 0.8. It is not a duty-cycle measurement.

The open gate rejected new 1024-token updates. It did not reject a new 2048 or 4096. Those 1024-token updates were not top-K rejects: the same cells' queues sit near a median of 6, under 16. That part is an admission change relative to coverage top-K alone.

Nothing in the log joins one of those 942 rejects to a later request, a route, or a TTFT. `COUNTERFACTUAL_NOT_VERIFIED` for "useful updates arrived earlier" and for "FN fell because of the filter". The 161.0 ms mean (sample SD 193.3, 20 pairs) is full Adaptive relative to StaticSemantic. StaticSemantic has top-K 0. The pair includes K=16, shrink-to-4, and the 1024 gate. ρ=0.5 inside that mean is −2.0 ms. The same contrast on original-compatible is −25.2 ms. It is not a utility increment.

Run B, commit `5d2d300`, fixed 10 Mbit, is the negative control. Adaptive and AdaptiveNoPriority, seeds 0 and 1, ultrahigh and burst: `congested_fraction = 0`, `relay_drop_low_utility = 0`, top-K drops from 3.3e5 to 5.2e6. Utility did not run. `UTILITY_EFFECT_NOT_EXERCISED` on that path.

## Figure 7(b)

`supplemental_20260918_sensitivity` panel (a) is a theta heatmap of full Adaptive against StaticSemantic at ρ=1.2. Panel (b) is one prefix length at a time, comparing RateFIFO, StaticSemantic, and Adaptive. Both arms that include Adaptive also include top-K and shrink. Neither panel is utility against a coverage threshold, or utility against StaticTopK16. A stable heatmap supports the bundle, not the freshness term.
