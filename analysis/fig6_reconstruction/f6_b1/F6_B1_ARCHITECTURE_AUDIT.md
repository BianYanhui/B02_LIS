# F6-B1 architecture audit

Parent at the start of this round: `3d5bf33` on `f6-d1-measured-routing-regret`. Work is on `f6-b1-real-invalidation`. Nothing in `experiments/4t4/` is edited. `b02-gateway4t4` on `127.0.0.1:9710` is not reconfigured.

The relay is a second process of the frozen `gateway_relay_4t4.py` on `127.0.0.1:9730`, forwarding to a dispatcher on `127.0.0.1:9731`. Merge, replica cap, adaptive utility, and coverage top-k are off in every cell. The only flag that changes between the two strategies is `priority`.

## Four states

Physical residency is what vLLM holds. The experimental adapter's tombstone is what this round emits after it sees a real clear. The gateway queue is the relay's unsent deque. The dispatcher index is what `choose` reads. Deleting a ShadowCache entry is not a physical loss. Deleting a dispatcher entry is not a physical loss. This round records those times separately.

## Priority, and where it stops

`Relay.enqueue` appends a `K_TOMB` to `pqueue` when `priority` is set, and to the FIFO otherwise. `_dequeue` pops `pqueue` before the FIFO. `release_loop` checks `max_inflight` before it dequeues. A frame already removed from `queued`/`pqueue` and written toward the socket is not pulled back. Priority is non-preemptive. It cannot reorder a frame that has entered the kernel.

`max_inflight` is the frozen control that keeps later frames unsent while one frame waits for an ACK. This round uses it only on the controlled arm. The ACK delay is computed from this pilot's own capture span. It is not the F6-A2 value 0.588 s, and it is not a 10 Mbit claim.

## Tombstone and choose

`Dispatcher.apply_tombstone` drops that owner and digest only. The other owner's entry stays. `choose` is unchanged: native least-loaded over every worker, then an affinity override only if the guard passes. At load 0 and `rr = 0`, a direct execution of the frozen function does this:

- Index coverage 4096 on workers 0 and 1: `choose` returns worker 0, override false. Native coverage is already 4096, so the guard does not move the request.
- After `apply_tombstone(0, X)`: `choose` returns worker 1, override true, because native worker 0 now has index coverage 0 and worker 1 still has 4096.

The stale entry changes the decision. It does not do so by winning an affinity sort over a longer prefix. It keeps native coverage positive, so the guard never leaves the stale owner. A counterfactual dispatcher with that entry removed is recorded beside the live call. The live call is the one that counts. The counterfactual is attribution, not a second online request.

## Invalidation adapter

`run_f6_b1.py` is marked `EXPERIMENTAL_INVALIDATION_ADAPTER`. It is not `publish_state`. It emits `K_TOMB` for owner 0 and lineage `F6B1S{seed}X` only after worker 0's event tap shows `AllBlocksCleared` ordered before any `BlockStored`, and no later store in that window carries prefix X. The clear is queued by vLLM until the next scheduler step, so a short trigger prompt that is not prefix X is sent only to publish the event. That trigger is recorded. It is not a refill of X.

The physical loss is a developer `reset_prefix_cache` on worker 0. Label: `CONTROLLED_REAL_CACHE_INVALIDATION`. It is not a natural `BlockRemoved`. No `BlockRemoved` is invented. A clear ordered after a store of X fails the trial.
