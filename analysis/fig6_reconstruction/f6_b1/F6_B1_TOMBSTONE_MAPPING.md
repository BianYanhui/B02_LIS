# F6-B1 tombstone mapping

The emitter is `EXPERIMENTAL_INVALIDATION_ADAPTER` in `run_f6_b1.py`. It is not `publish_state`, and it is not an automatic mapping from `BlockRemoved`.

## Rule

A `K_TOMB` is emitted only when all of the following hold for that seed:

- Worker 0 and worker 1 have a `BLOCK_CHAIN_MATCH` at 4096 tokens for lineage `F6B1S{seed}X`.
- Worker 0's tap then shows `AllBlocksCleared` with receive time `t_clear`, ordered before any `BlockStored` in that loss window.
- No stored token span in that window matches the first 32 tokens of prefix X.
- Worker 1 records no new clear and no removal.
- The reset HTTP status is 200.

The tombstone is owner 0, digest `digest64` of the lineage string, coverage 0. Generation time is `t_clear`, the tap receive time. There is no separate mapping delay after the native event: the adapter stamps the tombstone at that receive time. That zero gap is by construction. It is not a measured propagation delay.

The wire format has no epoch field. None was added. Each seed uses a new lineage string, so a tombstone cannot hit a previous trial's digest.

## What the tombstone does not mean

It withdraws the 4096-token advertisement for owner 0. It does not delete worker 1. After every completed relay cell, worker 1's index coverage was still 4096 and worker 0's entry was gone (`w0_cleared = 1`, `w1_kept = 1` on all 18 cells).

It is a full-coverage withdrawal because the reset drops reusable coverage to 0. It is not a partial-eviction update, and it is not described as a lossless shrink to a shorter prefix. No `BlockRemoved` on the X chain was observed, so no partial-survivor case is claimed.

Background upserts use other lineage strings. A tombstone for X does not supersede them. Merge is off, so nothing else is replaced in the queue either.

## Replay versus the capture

The capture is `OBSERVED_PHYSICAL_INVALIDATION`. The relay cells are `TIMED_RELAY_REPLAY`: the same background generation times and the same `t_clear` are replayed with their original gaps, after both X upserts have already been applied. Those two upserts are established before the timed section so the stale view exists. Their apply wait is not part of the stale-positive duration.
