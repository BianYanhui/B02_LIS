# Frozen 4x Tesla T4 supplementary experiment

This report uses only VALID formal rows. Confidence intervals and paired effects use repetitions as the statistical unit; individual requests are not treated as independent runs.

## Required findings

### original_compatible
- Adaptive vs RateFIFO: paired P95-age reduction 15.942 +/- 7.147 s; view-missing reduction 0.048 +/- 0.053; cached-token change 46.4 +/- 50.6 tokens/request; TTFT saving 172.6 +/- 69.4 ms.
- Adaptive vs LatestOnly: paired P95-age reduction 2.093 +/- 4.914 s; view-missing reduction 0.054 +/- 0.040; cached-token change 17.4 +/- 26.9 tokens/request; TTFT saving 4.7 +/- 73.4 ms.
- Adaptive vs AgeCov-Greedy: paired P95-age reduction 8.747 +/- 4.181 s; view-missing reduction 0.146 +/- 0.039; cached-token change 84.0 +/- 32.9 tokens/request; TTFT saving 23.0 +/- 74.3 ms.
- Adaptive vs StaticSemantic: paired P95-age reduction 1.017 +/- 5.207 s; view-missing reduction 0.056 +/- 0.036; cached-token change 14.3 +/- 24.8 tokens/request; TTFT saving -25.2 +/- 50.9 ms.
- Adaptive vs FullSync: paired P95-age reduction 8.138 +/- 4.406 s; view-missing reduction 0.158 +/- 0.047; cached-token change 77.5 +/- 31.4 tokens/request; TTFT saving -9.3 +/- 51.5 ms.

### reuse_intensive
- Adaptive vs RateFIFO: paired P95-age reduction 31.298 +/- 8.883 s; view-missing reduction 0.025 +/- 0.029; cached-token change 109.3 +/- 24.8 tokens/request; TTFT saving 391.0 +/- 88.7 ms.
- Adaptive vs LatestOnly: paired P95-age reduction 17.575 +/- 12.635 s; view-missing reduction 0.159 +/- 0.056; cached-token change 162.6 +/- 46.9 tokens/request; TTFT saving 172.1 +/- 60.9 ms.
- Adaptive vs AgeCov-Greedy: paired P95-age reduction 17.067 +/- 13.669 s; view-missing reduction 0.174 +/- 0.057; cached-token change 142.4 +/- 48.4 tokens/request; TTFT saving 117.9 +/- 58.6 ms.
- Adaptive vs StaticSemantic: paired P95-age reduction 4.427 +/- 13.835 s; view-missing reduction 0.053 +/- 0.054; cached-token change 101.3 +/- 52.9 tokens/request; TTFT saving 161.0 +/- 84.7 ms.
- Adaptive vs FullSync: paired P95-age reduction 27.456 +/- 11.811 s; view-missing reduction 0.180 +/- 0.048; cached-token change 182.4 +/- 43.5 tokens/request; TTFT saving 210.3 +/- 55.0 ms.

## Direct answers

1. **Adaptive vs RateFIFO:** yes for the main reuse-intensive workload under the same physical HTB budget: P95 state age improves by 31.30 +/- 8.88 s, cached tokens by 109.32 +/- 24.77 tokens/request, and mean TTFT by 391.04 +/- 88.71 ms. The analogous original-compatible P95-age effect is 15.94 +/- 7.15 s; this rules out an explanation based only on a lower physical signaling budget.
2. **Adaptive vs LatestOnly:** supersession alone is insufficient in reuse-intensive serving: Adaptive improves P95 age by 17.57 +/- 12.64 s, cached tokens by 162.64 +/- 46.88 tokens/request, and mean TTFT by 172.13 +/- 60.92 ms. On the original-compatible workload, the LatestOnly TTFT difference (4.67 +/- 73.40 ms) is not resolved by its 95% interval.
3. **Adaptive vs AgeCov-Greedy:** the simple age-coverage score is not sufficient for reuse-intensive serving: Adaptive improves P95 age by 17.07 +/- 13.67 s and mean TTFT by 117.85 +/- 58.58 ms. In original-compatible serving its TTFT difference (23.03 +/- 74.26 ms) is inconclusive, so this is not claimed as a universal latency win.
4. **Four-instance redundancy:** all four owners carried measured traffic, and the formal rows expose replica-suppression counters under 25% replica overlap. The 0/25/50/75% overlap sweep is calibration-only and remains NOT FOR PAPER; this run therefore demonstrates four-owner operation but does not claim a formal 3-to-4-instance effect size.
5. **Reuse-intensive TTFT:** yes, the strongest direct conversion is Adaptive versus RateFIFO (391.04 +/- 88.71 ms); positive paired TTFT savings also occur against LatestOnly (172.13 +/- 60.92 ms), AgeCov-Greedy (117.85 +/- 58.58 ms), StaticSemantic (160.99 +/- 84.73 ms), and FullSync (210.25 +/- 55.01 ms).
6. **Original-compatible TTFT:** freshness remains stronger evidence than latency generality. Although Adaptive improves P95 age versus RateFIFO by 15.94 +/- 7.15 s, its TTFT shifts versus LatestOnly, AgeCov-Greedy, StaticSemantic, and FullSync have intervals that include zero; they should not be promoted as stable serving-latency gains.
7. **Cached-token buckets:** yes. Fig. C and `ttft_by_reuse_bucket.csv` show substantially lower TTFT for long actual vLLM reuse than for the 1-511-token bucket. Empty buckets are retained rather than interpolated, so the plot does not manufacture a monotonic curve where the workload has no samples.
8. **Dynamic recovery:** all five repetitions recovered within the 45-s observation window for every policy, but Adaptive was not the shortest mean recovery time (19.10 s); LatestOnly was fastest at 16.34 s. Thus this dynamic run supports controlled recovery, not a claim that Adaptive universally recovers fastest.
9. **High churn:** yes. The churn experiment injected stale positives, owner cache resets, and delayed tombstones; it recorded 238 normal-prefill fallback events. The separate live owner runtime check contains 896 native fallback decisions across four endpoints, with 0 unsafe reuses.
10. **Safety:** all VALID formal rows have zero `incorrect_kv_reuse_count` and zero request-error rate; the four native endpoint checks also passed scope/version/lease/eviction/restart scenarios with zero unsafe reuse.
11. **Mechanism attribution:** replacement alone (LatestOnly) and simple utility ranking (AgeCov-Greedy) leave a positive reuse-intensive gap, supporting the value of the complete semantic mechanism. StaticSemantic narrows that gap; its P95-age difference is not robust in this sample, so the separate incremental contribution of adaptive admission beyond merge, urgency, and replica suppression should be reported as limited rather than universal.

## Frozen RateFIFO calibration

The pre-registered calibration-only selector chose a 1-frame token-bucket burst. Its rule and all candidate scores are stored in `analysis/formal4t4/calibration/ratefifo_selection.json`.

