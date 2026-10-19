# Mechanism evidence

The ov30 FN gap is real at request level and unexplained at update level. The base pair shows the same control-path difference with no user-path difference. Hypotheses are checked against those two facts.

## H1. K=16 keeps low-value updates and loads the path

`OBSERVED` as a load difference. On base, K=16 forwards about 9988/s against about 7895/s for K=8. Gateway CPU is 0.625 against 0.572. Peak queue is 17 against 8 or 9. Dispatcher frames equal gateway forwards on every cell, so the extra frames are applied, not just written.

`NOT_SUPPORTED` as a user cost on base. Reuse and TTFT do not move with that load.

On ov30 the same pattern exists: K=16 forwards about 10150/s, K=8 about 7880/s, and K=16 leaves more foreground updates undelivered. The extra applied frames are mostly noise (`noise_frames` is the difference). That does not identify which noise frame displaced which useful frame.

## H2. Those updates crowd out a valuable send

`NOT_SUPPORTED` on base. Both K apply 143 of 143 foreground updates.

`CAUSAL_CHAIN_NOT_VERIFIED` on ov30. Undelivered foreground counts are higher at K=16 (13, 20, 20 versus 7, 4, 3). The logs do not join a dropped digest to the request that later missed it. There is no rank-17 coverage distribution.

## H3. K=8 delivers the valuable update sooner

Foreground lag P95 is lower for K=8 on every ov30 seed and every base seed, by about 9 ms. On base that earlier delivery changes nothing the request can see: FN stays 0 and reuse matches. On ov30 the lag gap co-occurs with the FN gap. Nine milliseconds against a 1700 ms TTFT is not, by itself, the TTFT result. `CAUSAL_CHAIN_NOT_VERIFIED`.

## H4. The FN drop is what improves TTFT

`NOT_SUPPORTED`. ov30 FN is lower for K=8 on all three seeds. Mean TTFT is lower on two and higher on seed 0 by 18 ms. Base has no FN on either side and no TTFT movement. Routing FN and user TTFT are separate outcomes in these cells.

## What the request rows do show on base

Same request id, same slot, same coverage, 133 scored requests, zero mismatches. Routes still differ: 32, 9, and 1 requests. Reuse differs on one request in seed 0 and on none in the other seeds. A different route is not a different cache outcome here.
