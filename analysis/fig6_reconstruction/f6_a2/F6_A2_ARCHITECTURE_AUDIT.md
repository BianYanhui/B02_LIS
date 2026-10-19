# F6-A2 architecture audit

Parent commit at the start of this round: `c999630` on `f6-f1-formal-ac`. Work is on `f6-a2-real-extension`. Nothing in `experiments/4t4/` was edited. `b02-gateway4t4` on `127.0.0.1:9710` was not reconfigured.

## Three layers

### Native block events

vLLM 0.10.2 emits events from `vllm/v1/core/block_pool.py`.

- `cache_full_blocks` emits one `BlockStored` for the new full blocks of a step. That object can carry many block hashes, their parent hash, and the token ids of those blocks. Block size in this experiment is 16.
- `_maybe_evict_cached_block` emits `BlockRemoved` for one hash when a free cached block is reused. A hit (`touch`) emits nothing.
- `reset_prefix_cache` emits `AllBlocksCleared`. This experiment does not call it.

`ZmqEventPublisher` in `vllm/distributed/kv_events.py` sends one ZMQ multipart message per non-empty batch: topic, 8-byte sequence, msgpack payload. One message is not one block hash. P0-C1 already measured this on one T4: 212 ZMQ messages, 252 event objects, 6615 hash occurrences. That collapse is upstream of the gateway.

### Wrapper prefix advertisement

The formal harness `publish_state` in `experiments/4t4/run_formal4t4.py` does not read `BlockStored`. After a request completes it sends one `K_UP` whose digest is the lineage string `L{lineage_id:04d}` and whose coverage is the number assigned by the trace. For `reuse_intensive`, `reuse_prefix_lengths` pins each lineage at 1024, 2048, or 4096 for the whole run. Later advertisements of that digest repeat the same coverage. F6-F1 counted 0 coverage-increasing supersessions for that reason.

The wire header (`>BBHIqQd`) has kind, instance, cell, seq, coverage, digest64, and `t_send`. There is no version field. `digest64` is blake2b of the lineage string, not a hash of the token ids. The frozen gateway merges on `(instance, digest)` in `_supersede_queued_up`. It replaces any unsent upsert for that key. It does not compare coverage and it does not merge across owners.

This round's adapter lives only in `analysis/fig6_reconstruction/f6_a2/run_f6_a2.py`. It is marked `EXPERIMENTAL_WRAPPER`. It is not `publish_state` and it is not deployed. It keeps the frozen key: one stable lineage string per growing prefix, owner in the instance field, coverage in the coverage field, order in `seq`. It does not put the full-content hash into the digest, because that would make the three versions three different keys and the frozen rule would not treat them as one prefix. The token-prefix proof is recorded beside the wire key. The adapter emits one upsert when each real request returns. It does not hold the three versions and release only the last one.

`Dispatcher.apply_upsert` overwrites that lineage's coverage with whatever frame arrives. It does not refuse an older coverage. That behavior was not changed. A regression, if one happens, is reported.

### Gateway supersession

`Relay.enqueue` calls `_supersede_queued_up` only when `merge` is set and an unsent upsert for the same `(instance, digest)` is still in `queued_up`. The old bytes stay in the deque as a ghost. `_pop_queue` skips them. The drop reason is `superseded`.

A frame that `_dequeue` has already removed from `queued_up` cannot be superseded. The release loop dequeues before the socket write. TCP backpressure and a tc backlog after that write are too late for this rule. The only frozen control that keeps a frame pending is `max_inflight`: while `recent` is full, `release_loop` does not dequeue. This experiment uses that window on an isolated relay. It does not use the F6-P3 HTB formula, which remains `MODEL_REJECTED`.

Priority, replica cap, adaptive utility, and coverage top-k are off in both arms. The isolated relay listens on `9720` and connects to a dispatcher on `127.0.0.1:9721`.

## What this round can and cannot attribute

Block-hash to ZMQ-message reduction is Stage W. It is not gateway supersession.

Gateway `TRUE_EXTENSION` is Stage G: the previous coverage for the same owner and lineage was still pending, and the new coverage is strictly larger.

Same-coverage replacement is the F6-F1 phenomenon. It is counted separately.

A controlled ACK window is not the natural four-GPU bottleneck. P0-C1's conclusion stands: the measured block-event workload did not fill the indexer or a 10 Mbit path.
