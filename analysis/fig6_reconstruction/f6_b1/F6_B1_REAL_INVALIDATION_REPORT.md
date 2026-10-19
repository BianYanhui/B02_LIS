# F6-B1 real invalidation

Label: `CONTROLLED_REAL_CACHE_INVALIDATION`. This is a developer `reset_prefix_cache` on worker 0, followed by one 64-token trigger that is not prefix X. It is not a natural `BlockRemoved`. The loss window on worker 0 recorded `removed = 0` in every seed. No removal event was synthesized.

Four captures, seeds `2026101919` (pilot) and `2026101920`–`2026101922`. Each one passed the residency gate.

## What was held before the reset

Workers 0 and 1 each completed the same 4096-token prefix. Evidence on all eight target rows is `BLOCK_CHAIN_MATCH`. Warmup `cached_tokens` is 0, which is the cold fill after `reset_prefix_cache`. The event order is a clear and then the stores. A clear before the first store is the deferred reset publication, the same rule fixed in F6-D1. Prompt length of the later routing request is 4128 (the 4096-token prefix plus 32 suffix tokens). Token identity is in `raw/captures.json` (`prefix_sha256`, `first_prefix_ids`, and the token lists).

## The loss

Only worker 0 was reset. The HTTP status was 200. Worker 0 then published `AllBlocksCleared` on the next scheduler step, which the trigger provides. The trigger's stored tokens do not match prefix X (`x_tokens_after = 0`).

| Seed | Reset command → HTTP done | HTTP done → native clear | Command → native clear |
| --- | ---: | ---: | ---: |
| 2026101919 | 5.87 ms | 5.32 ms | 11.19 ms |
| 2026101920 | 4.88 ms | 5.08 ms | 9.97 ms |
| 2026101921 | 5.62 ms | 4.68 ms | 10.30 ms |
| 2026101922 | 4.45 ms | 4.86 ms | 9.31 ms |

The native clear is not the physical-loss instant. vLLM queues `AllBlocksCleared` until the next scheduler step. The table keeps the reset command, the HTTP return, and the tap receive time separate. The trigger is recorded and is not a refill of X.

Worker 1's tap showed no additional clear and no removal between the reset command and the end of the capture. That is retention evidence from the event stream. A direct `cached_tokens` read of worker 1 in that same world was not taken, because that request would have been another prefill. The executed cleared worlds, rebuilt from a cold reset, did return `cached_tokens = 4096` on worker 1. Those are separate trials.

## Background upserts

Eight real 256-token completions ran on workers 2 and 3. All eight scored `BLOCK_CHAIN_MATCH` after the chain walk was allowed to follow a parent block stored by the earlier request on that worker. The first request on a worker is cold (`cached_tokens = 0`). The later ones reuse one 16-token block (`cached_tokens = 16`) because their text prefixes share that block. The lineage strings still differ, so the eight gateway messages are eight digests. They are not prefix X.

On seeds `2026101920` and `2026101921`, the first background on workers 2 and 3 shows three `reset_clear` events before the store. Workers 2 and 3 had not taken a scheduler step during the two executed worlds, so those earlier `reset_prefix_cache` calls stayed queued and were published together. They are prior resets, not three evictions of the new prefix. The following store still matched.

The first pilot, saved under `raw/pilot_shared_prefix_window/`, stopped before any relay cell because a fresh event window could not see that cached parent and marked six backgrounds `PARTIAL`. The target loss in that pilot was already real (`lost = 1`, both targets matched). The scorer change does not touch the relay or `choose`.
