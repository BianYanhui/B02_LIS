# F6-B smoke report

## Verdict

**INCONCLUSIVE**

Invalidation priority can change relay-queue order, and that order can change a routing decision inside a constructed window. This round did not observe a real stale-positive on vLLM. No GPU smoke was run.

## Why there is no vLLM smoke

The offline check was required first. It uses the frozen `Relay._dequeue` and the frozen `Dispatcher.choose`. It does not establish that a live worker had lost the prefix.

A short vLLM run would not close that gap. Formal `publish_state` emits `K_TOMB` only from ShadowCache, and the formal baseline sent 0 tombstones. P0-A4’s BlockRemoved trace was never applied to this dispatcher. Owner validation is a separate microbenchmark. Starting the four T4s for up to 120 seconds would not produce an aligned timeline unless the harness were instrumented and a cache-fill workload were added. That is a new experiment, so it was not started. GPUs were idle (0 MiB) at the start of this round and were not used.

## What was run

Script: `analysis/fig6_reconstruction/f6_p0/smoke_b/offline_priority.py`

Table: `analysis/fig6_reconstruction/f6_p0/smoke_b/priority_window.csv`

Label: **CONTROLLED**. The prefix is stipulated to be already gone on worker 0 when the tombstone is enqueued. Worker 0’s dispatcher entry is still 2048 tokens. Eight unrelated upserts sit ahead of the tombstone. Loads are `[5, 0, 0, 0]`. Merge, dedup, adaptive, and top-k are off. The only difference is `relay.priority`.

Service is one `_dequeue` per step. This is the pre-kernel relay queue. It is not tc, not RTT, and not TTFT. Queue position is a slot count, not a measured second.

`stale_positive` here means `Dispatcher.choose` returned affinity true, view coverage ≥ 512, and the stipulated truth coverage is 0.

## Result

| Strategy | Tombstone slot | Request after 0 frames | Request after 1 frame | Request after the tombstone is applied |
| --- | ---: | --- | --- | --- |
| FIFO | 8 | stale, select worker 0 | stale, select worker 0 | not stale, select worker 1 |
| Priority | 0 | stale, select worker 0 | not stale, select worker 1 | not stale, select worker 1 |

At one service step the two orders disagree. FIFO still routes to worker 0 on a positive affinity entry. Priority has applied the tombstone and routes to an idle worker with affinity false. Before any service, both are stale: the request sits in the window and priority has not yet run. After the tombstone is applied, both agree.

The estimated net on the stale decision is 30.96 ms (`2048/50 - 5*2`). That number is the dispatcher’s guard input. It is not a measured TTFT.

## What this does not show

- No BlockRemoved, no `vllm_cached_tokens`, no owner-validation call. Truth coverage 0 is stipulated.
- No sample of visibility delay in seconds. The formal baseline’s p95 of 0 is an empty-sample placeholder.
- The churn table’s ~170 s tombstone p95 is an injected-reset cell with bundled policies. It is not this contrast.
- Priority does not reorder frames already written to the kernel. A backlog in tc can leave both strategies stale. That path was not exercised.

## Case check

B-PASS would need a shorter visibility delay and fewer stale-positive routing attempts on real residency. The delay here is a queue slot, and the residency is stipulated. Not B-PASS.

B-PARTIAL would mean the delay fell and the routing decision did not change. In this constructed window the decision does change. The formal traces never entered the window (`source_tombstones_sent` = 0). Not B-PARTIAL.

B-FAIL would mean priority cannot be isolated, or a real invalidation cannot be recognized in principle. The flag can be isolated, and `physical_false_positive_affinity` is the right column when a tombstone and a request actually meet. The meeting was not recorded. Not B-FAIL.

The round is **INCONCLUSIVE** for the paper claim. A formal Fig. 6(b) matrix is not justified until one logged run contains a real loss of residency, a still-positive dispatcher entry, and a request inside that interval, with every other admission flag held fixed.
