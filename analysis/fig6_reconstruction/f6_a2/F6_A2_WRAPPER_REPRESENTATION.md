# F6-A2 wrapper representation

The adapter is `EXPERIMENTAL_WRAPPER` in `run_f6_a2.py`. It is not `publish_state`.

## Counts

One capture process, four workers, feasibility plus three formal seeds.

| Quantity | Count | What it is |
| --- | ---: | --- |
| ZMQ messages | 168 | One publisher batch each |
| `BlockStored` events | 168 | One event per request |
| Block-hash occurrences inside those events | 14336 | 64 + 64 + 128 hashes per lineage |
| `BlockRemoved` | 0 | |
| `AllBlocksCleared` | 0 | Reset was not called |
| Wrapper prefix upserts | 168 | One per completed request |

14336 / 168 = 85.333 hashes per ZMQ message. That packing is vLLM putting every new full block of one step into a single `BlockStored`. It is not a gateway merge, and it is not a reduction in ZMQ messages. Message count and wrapper upsert count are the same: 168 and 168.

Per lineage the three upserts are coverage 1024, then 2048, then 4096, emitted when that request returns. The adapter does not hold the three versions and send only the last.

## Semantic boundary

The wire digest is blake2b of the stable lineage string, which is the frozen gateway key. It is not the hash of the full token list. The token-prefix proof is the chain match in `f6_a2_source_growth.csv`. Sequence on the wire is the send order. Lineage version 1, 2, 3 is in the trace, not in the header. The header has no version field, and none was added.

No eviction was observed, so this does not show that a prefix upsert preserves a partially evicted ancestor. P0-C1's lost-partial-prefix case remains a separate limit.
