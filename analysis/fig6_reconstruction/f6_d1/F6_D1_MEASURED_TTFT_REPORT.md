# F6-D1 measured TTFT

Client TTFT is the streamed POST until the first non-empty completion `text`. Empty events and the usage chunk are not a token. Server TTFT is `NOT_MEASURED`. The dispatcher cost is not a TTFT.

Label of the delays below: `MEASURED_CLIENT_TTFT`. The pairs are repeated-trial counterfactuals. Full and Cap rebuild the same token ids after `reset_prefix_cache`, with the world order crossed. They are not two outcomes at one instant. Pilot seed `2026101909` is not in these means. All 12 formal pairs and both D3 pairs passed the preregistered checks: block chain, cold warmup, owner set, zero or held gauges, prompt length 4128, and response cached tokens equal to the selected warmup coverage.

## Formal pairs, load 0

| Scenario | Valid pairs | Mean Full ms | Mean Krep=2 ms | Mean Δ ms | Median Δ ms | Sample SD | Min | Max | Coverage loss | Route change | Model regret ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 equal 4096 | 4 | 410.23 | 93.26 | -316.97 | -1.10 | 633.05 | -1266.54 | 0.86 | 0 | 0.50 | 0 |
| D1 hidden has 4096 | 4 | 92.20 | 1904.80 | 1812.59 | 1802.58 | 90.32 | 1716.12 | 1929.09 | 3072 | 1.00 | 61.44 |
| D2 visible has 4096 | 4 | 82.95 | 83.86 | 0.91 | 0.58 | 1.10 | -0.02 | 2.50 | 0 | 0 | 0 |

Δ is Krep=2 minus Full. Positive means the capped view's request was slower. Coverage loss is response cached tokens of the Full worker minus those of the Krep=2 worker. There is no significance test at n = 4.

D1, every formal seed:

| Seed | Assignment | Order | Full worker | Cap worker | Full ms | Cap ms | Δ ms |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026101910 | A | Full then Cap | 3 | 0 | 121.2 | 1837.3 | 1716.1 |
| 2026101911 | B | Cap then Full | 0 | 3 | 81.9 | 1910.9 | 1829.0 |
| 2026101912 | A | Cap then Full | 3 | 0 | 83.7 | 1859.8 | 1776.2 |
| 2026101913 | B | Full then Cap | 0 | 3 | 82.0 | 2011.1 | 1929.1 |

Assignment A mean Δ is 1746 ms. Assignment B mean Δ is 1879 ms. Both orders stay positive. The long-cache request lands on both GPU 0 and GPU 3, and the short-cache request does too. The gap is not one device being slow.

D2 deltas sit between -0.02 ms and 2.5 ms. The views select the same worker, and both hit 4096 tokens.

## The D0 pair that dominates the mean

Seed `2026101910`, scenario D0, is the first formal world. Both views select worker 0, both return cached 4096, gauges are 0, and the model regret is 0. Full TTFT is 1390.4 ms and Cap TTFT is 123.9 ms, so Δ is -1266.5 ms. The route did not change. This is not a replica-suppression benefit.

The other three D0 deltas are -1.39, +0.86, and -0.80 ms. Two of those pairs are assignment B, which does switch GPUs (worker 0 versus worker 3) at equal coverage, and the TTFT still matches. The D0 median is -1.10 ms. The mean is -316.97 ms only because of the first pair. That pair stays in the primary table.

Difference in differences, D1 minus D0, is +2129.6 ms on the means and +1803.7 ms on the medians. The mean contrast is inflated by the D0 first pair. D2 minus D0 is +317.9 ms on the means and +1.7 ms on the medians. A post-hoc mean that drops only that first D0 pair is -0.44 ms for D0 and +1813.0 ms for D1 minus D0. It is not a replacement for the primary mean.

The pilot D1 pair, excluded from the means, was the first measured traffic on the boot: 1550 ms at cached 4096 and 3406 ms at cached 1024. Later long-cache hits settle near 83 ms. The first formal pair is the same kind of early delay, on an equal-coverage control, and it did not repeat.

## Model cost

`T = load * 2 + max(0, prompt_tokens - warmup_coverage) / 50`, regret `T(cap) - T(full)`. Label: `MODEL_DERIVED`.

On every D1 pair the regret is 61.44 ms. The measured mean Δ is 1812.59 ms, about 29.5 times the model term. The frozen 50 tokens/ms constant is not this T4's prefill rate. The model has the same sign as the measurement and is not a calibrated TTFT. It is not quoted below as the serving penalty.

## D3

Two pairs, real holders, not in the table above. Δ is 4072 ms (assignment A) and 5485 ms (assignment B). Coverage loss is 3072. Model regret is 63.44 ms because the capped worker also carries one queue penalty. Assignment A's full-view hit was 1426 ms rather than the ~84 ms seen on the other long-cache hits, so the two deltas are not tight. n = 2. This is a joint load-and-coverage observation, not the primary claim.
