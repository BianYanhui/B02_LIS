# P0-D3 utility implementation audit

Phase 1 name: utility implementation audit. No gateway code was changed. No GPU run.

The executable rule is `Relay.low_utility` in `experiments/4t4/net/gateway_relay_4t4.py`. `FRAME` is 64 bytes. Coverage is in tokens. The test is

`exp(-(age + ewma_dq) / tau) * coverage - lambda * 64 <= 0`

with `tau = 30` and `lambda = 16`. The bar is 1024 tokens when age and delay are zero. A 1024-token upsert fails for every non-negative age. A 2048-token upsert fails only when `age + ewma >= 20.79 s`. A 4096-token upsert fails only when that sum is at least `41.59 s`. The comparison is `<= 0`, so the equal case is a reject.

`low_utility` returns false immediately when `congested()` is false. `congested()` returns false when the adaptive flag is off. On current code the delay path is immediate: EWMA above `--gate` (default 2 s) opens it. The queue path latches only after depth stays at or above 8 for 0.2 s, and it drops after depth stays at or below 2 for 0.5 s. The formal4t4 binary at `1dc24ec` had no hold. There `congested()` was true as soon as EWMA exceeded 2 s or `len(queue) >= 8`.

Order inside `enqueue`, for an upsert:

1. rate limit
2. bounded-queue cap, mode 6 only
3. utility, only if congested
4. coverage top-K admission
5. merge supersession
6. replica cap
7. FIFO append, or the priority queue if the frame is a tombstone

Utility does not reorder the FIFO. It only removes. Tombstones do not enter `low_utility`. They are not filtered by the formula. With priority they go to `pqueue` and are dequeued first.

A second check exists in `release_loop`: an upsert that already passed admission can still be dropped, with reason `low_utility`, at the moment it is dequeued, if `congested()` is true and the formula now fails. That is a later decision, not the admission decision.

Top-K and utility have separate counters on the current tree. `trim_global_topk` increments `global_topk` and writes reason `global_topk`. At `1dc24ec`, the same trim incremented both `global_topk` and `low_utility`, and the event reason was `low_utility`. The formal4t4 cell column `relay_drop_low_utility` is that mixed counter. It is not a utility-admission count. The split below uses the gateway events.

`global_topk` is the pending digest cap. While `congested()` is true the cap becomes `max(1, K // 4)`. That shrink is not the utility test. StaticSemantic in formal4t4 has `gateway_global_topk = 0`.

P0-B1 (`aa97eec`, not an ancestor of this branch) already separated pre-enqueue rejects from post-enqueue events and recorded that Run B's latch count is zero. This round recomputed the events and split the post-enqueue ages. It does not replace that report.
