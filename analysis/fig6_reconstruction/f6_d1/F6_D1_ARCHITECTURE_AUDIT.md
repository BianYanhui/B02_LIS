# F6-D1 architecture audit

Parent commit at the start of this round: `5c6bb90` on `f6-a2-real-extension`. Work is on `f6-d1-measured-routing-regret`. Nothing in `experiments/4t4/` was edited. `b02-gateway4t4` on `127.0.0.1:9710` was not reconfigured.

The live relay for this round is a second process of the frozen `experiments/4t4/net/gateway_relay_4t4.py`, listening on `127.0.0.1:9720` and forwarding to a dispatcher on `127.0.0.1:9721`. Merge, priority, adaptive admission, coverage top-k, and the rate limiter are off. The only mechanism that differs between the two views is `dedup`: 0 for Full View and 2 for Krep=2.

## What replica suppression hides

`Relay._admit_replica` runs inside `enqueue`, before a `K_UP` is placed on the FIFO. With `dedup=0` every owner is admitted. With `dedup=2` the relay keeps the first two distinct instance ids for that `digest64`. A later owner is dropped with reason `duplicate_holder` and counted in `drops["replica_cap"]`. The holder set is not cleared when the frame is forwarded. `do_reset` clears it. Arrival order, not coverage and not load, decides who stays.

The dispatcher index is updated only from frames that arrive on the downstream socket. A dropped advertisement never becomes coverage. `Dispatcher.apply_upsert` overwrites that owner's coverage. It has no monotonic guard. This round does not change that.

The experimental adapter lives only in `analysis/fig6_reconstruction/f6_d1/run_f6_d1.py`. It is marked `EXPERIMENTAL_WRAPPER`. It is not `publish_state`. It sends one `K_UP` per owner after a real warmup, with coverage taken from that warmup's block-aligned prompt length. The digest is the lineage string `F6D1S{seed}{scenario}`, shared by all four workers. It is not a hash of the token ids. The token-id hash is recorded beside the wire key.

Admission order is serial and preregistered: visible owners, then hidden owners. The label is `CONTROLLED_ADMISSION_ORDER`. It is not a claim about natural arrival order.

## What Dispatcher.choose actually selects

`choose` is the frozen function in `experiments/4t4/run_formal4t4.py`. Parameters are the formal defaults: `j=4`, prefill `50` tokens/ms, queue penalty `2` ms, guard `0.5` ms.

1. Native selection is least-loaded over every worker, including workers whose advertisement was dropped. Ties break by `rr`, which starts at 0 in each new dispatcher and advances on every least-loaded call.
2. Affinity candidates are only workers whose index coverage for this digest is greater than 0. They are sorted by coverage descending, then load, then worker id, and truncated to `j`.
3. The affinity target is the least-loaded worker among the best visible coverage.
4. The override fires only when `incremental_tokens / 50 - max(0, load_target - load_native) * 2 > 0.5`. Otherwise the native worker is kept.

Replica suppression therefore hides KV affinity. It does not remove a worker from native load balancing. A hidden worker can still be the native choice. If that worker has no index entry, its coverage is read as 0, and a visible shorter prefix can steal the decision when the guard passes.

This round calls `choose` once per world and sends the streamed request to the returned worker. A read-only mirror of the same rule is logged beside it. The mirror is not allowed to replace the returned worker.

## Code-path expectations at load 0 and rr 0

These rows are a direct execution of the frozen `choose` on an in-memory index. They are not GPU measurements. Live trials must record the worker `choose` actually returned.

| View | Index coverage (workers 0–3) | Native | Selected | Override |
| --- | --- | ---: | ---: | --- |
| D1 A full | 1024, 1024, 4096, 4096 | 0 | 3 | yes |
| D1 A cap | 1024, 1024, 0, 0 | 0 | 0 | no |
| D1 B full | 4096, 4096, 1024, 1024 | 0 | 0 | no |
| D1 B cap | 0, 0, 1024, 1024 | 0 | 3 | yes |
| D0 A full and cap | 4096 on every admitted owner | 0 | 0 | no |
| D0 B cap | 0, 0, 4096, 4096 | 0 | 3 | yes |
| D2 A full and cap | long coverage on 0 and 1 | 0 | 0 | no |
| D2 B full and cap | 1024, 1024, 4096, 4096 or the cap's visible slice | 0 | 3 | yes |

D0 assignment B is the case where the route changes even though every worker's true coverage is 4096. The cap reads the native worker's coverage as 0, the guard passes, and the request moves to a visible owner with the same cache. That is a GPU-identity change, not a coverage loss.

D1 is the case where the selected workers also differ in true coverage (4096 versus 1024).

## Model cost, kept separate from TTFT

The formal cost, fixed before any trial:

`T = load * 2 + max(0, prompt_tokens - warmup_coverage) / 50`

`Regret = T(Krep=2 selected) - T(Full selected)`.

Label: `MODEL_DERIVED`. A startup tripwire checks that D1 assignment A at load 0 still returns workers 3 and 0. If that frozen result changes, the run stops before any GPU request. The cost model is not edited to create regret.

## Live check

The successful run called the frozen `choose` on the index the isolated relay actually delivered. Every returned worker matched the read-only mirror. For load 0 the live selections were the table above: D1 assignment A full selected worker 3 and the cap selected worker 0; D1 assignment B full selected worker 0 and the cap selected worker 3. On assignment B the cap's native choice was worker 0, whose advertisement had been dropped, and the guard then moved the request to worker 3. Native load balancing was not blocked from naming a hidden worker. The override is what left that worker.
