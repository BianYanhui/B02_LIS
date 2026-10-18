# Next iteration

Do not start this. The recommendation is one complementary cell, not an overlap matrix.

## Verdict

**P1-PASS.** Real four-worker residency, real in-flight imbalance, original `Krep=2` rule, and a 2.0 ms model regret on the same snapshot. The regret is not a TTFT.

## Answers

**Did this cell use four real vLLM workers?**

Yes. Ports 8000–8003, one Tesla T4 each, Qwen2.5-1.5B-Instruct, automatic prefix caching. Active measurement 14.7 s after startup.

**Was KV residency confirmed on all four owners?**

Yes. Warmup cached tokens were 0. The probe and the post-window reprobe both returned 1056 cached tokens on every worker, equal to the block-aligned common prefix. Each probe increased that worker’s `prefix_cache_hits` by 1056. ShadowCache was not used. Engine block hashes were not read.

**Was there a real load imbalance?**

Yes. Engine-confirmed harness loads were `[1,1,0,0]` and `[1,1,1,0]`, plus the balanced `[0,0,0,0]` and `[1,1,1,1]`. Later samples disagree with `num_requests_running` and are not used as engine-confirmed imbalance.

**Did `Krep=2` hide an owner?**

Yes, on every snapshot. The frozen first-two-holders rule hid workers 2 and 3. Advertisements went from 4 to 2.

**Was routing regret positive?**

Yes, as a model cost, on B1 and B2: 2.0 ms. Four of eight imbalanced samples are 0, and those rows were kept. Balanced regret is 0 even where the selected index changes.

**Is that regret a model estimate or a measured TTFT?**

Model estimate. `T_i = load_i * 2 ms + max(0, 1083 - 1056) / 50`. The prefill term is 0.54 ms on every owner. The 2.0 ms is one in-flight request times the formal queue weight. Probe TTFT was about 1.6 s and the later reprobe about 95 ms; neither is the unexecuted view’s TTFT.

**Is a replica-overlap matrix worth running?**

No. This cell is one prefix on all four workers. Overlap was not a factor. The positive regret is the arrival ramp of a round-robin that starts at worker 0, which is the same order the cap keeps. A sweep of overlap fractions would not separate that alignment from the cap.

**If anything is extended, what is the minimal matrix?**

One more live cell, not a matrix. Reverse the holder launch order to `[3,2,1,0,3,2,1,0]`, leave admission order at 0,1,2,3, and keep a sample only when `num_requests_running` equals the harness count. Same prefix, seed 1, full view and `Krep=2` offline. Do not retune the cap if the light worker is visible. Do not drop the zero-regret samples.

That is 1 GPU cell and 2 offline views. No ρ sweep, no overlap sweep, no extra seed, no Fig. 6(b).

**GPU time and cost**

Startup of the four servers is about 1–2 minutes. The measurement itself was 15 seconds and should stay under 2 minutes. Stop if the warmup wave exceeds 120 seconds. No network shaping and no extra GPU beyond the four T4s already used.

Fig. 6(b) stays closed. Do not draw Fig. 6. Do not edit the paper.
