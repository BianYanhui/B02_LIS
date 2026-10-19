# F6-F1 Fig. 6(c) — Formal replica-overlap signaling experiment

## Question

How many redundant KV metadata advertisements does replica cap `Krep=2` remove as the phase-0 replica overlap changes? The measured quantity is signaling reduction on one offered stream. Routing regret, TTFT, and pressure feedback were not run.

## Fixed configuration

- 4 live vLLM 0.10.2 workers, one per Tesla T4, `Qwen2.5-1.5B-Instruct`.
- Automatic prefix caching. Shadow capacity read from the server log: 104,544 tokens, matching the audited value.
- Pool 32, workload `reuse_intensive`, 48 requests, warmup 12, output length 4, concurrency 4.
- Replica cap in the replay: 2. Live recording cap: off.
- Overlap levels `0.00`, `0.25`, `0.50`, `0.75`, defined as `round(32 * overlap)` phase-0 prefixes requested and advertised on all four workers. Overlap is not a repeated-message fraction.
- Seeds, used only as a set: `2026101901`, `2026101902`, `2026101903`. Calibration seed `2026072640` is not in the mean.
- Cache salt per cell: `f6f1:{seed}:{overlap}`. Relay reset between cells. No `reset_prefix_cache`. No HTB change. `cell_rate_4t4.sh` was not run.
- Mechanisms off on the live cell and on both replays: merge, priority, adaptive filtering, coverage top-k. The live cell also has replica suppression off.
- Cell ids `41000`–`41011`. One vLLM boot for all 12 cells. Active cell time 399.7 s.
- Parent commit at execution: `7605cd5d7d3679e3c6a6aaf8ccc5f6ba13e1d513`.
- Driver: `run_f6_c_live.py`, sha256 `0c04d4f47e8d1b069272394dc8a94a36720be18c59267f350cd7a7df7a063acf`.
- Replay: frozen `Relay.enqueue` / `_admit_replica` through F6-P3 `replay_cap` (`run_ac_readiness.py`, sha256 `b9220f8595652cfede01f7bd6414c87143d7b257926b53c7f535ed211aa4a827`). Both arms are full-drained.

The ambient gateway class was already `htb 1:1` at 10 Mbit. This experiment did not set that rate and does not treat it as a production bottleneck. Live window-end backlog was 0 on every formal cell, so the cap result does not depend on draining a leftover queue.

## Capture

The long-running `b02-gateway4t4` process was started with `quiet=True`, so `emit_update` does not print per-update JSON. The container was not restarted and its HTB was not edited.

Each offered frame was recorded on the dispatcher socket after the real relay admitted and released it. FullSync is FIFO and does not reorder. For every formal cell, relay `K_STATS` / `K_STATS2` reported `received = forwarded = link_sent`, with `rate_limit`, `superseded`, `replica_cap`, `low_utility`, `queue_drop`, `expired`, `global_topk`, and `stale_cell` all 0, and `queued = live_depth = 0`. Under that accounting, dispatcher release order is the gateway admission order.

`gateway_arrival_time` itself is `NOT_MEASURED`. The stored timestamp is `dispatcher_recv_time`. Replica cap uses `recv_index`, not worker id and not the harness send sequence. Send sequence is not assumed to be `0,1,2,3`.

Evidence labels: `LIVE_VLLM_OFFERED_TRACE` for the offered stream, `PAIRED_FROZEN_RELAY_REPLAY` for both strategy arms. The `Krep=2` column is not a live four-GPU serving run with the cap enabled.

An earlier instrumentation pass (`failures_capture_r1.json`) ran seed `2026101901` at overlap 0 twice, looked for gateway JSON that the quiet relay does not emit, and recorded offered 0. Those two attempts are not in the formal table. The formal matrix then completed 12/12 cells on attempt 1 with the cold salt above. No seed was replaced because of the result.

## Per-cell results

All 12 cells are `VALID`. Tombstones: 0. `Forwarded_NoSuppression - Forwarded_Krep2 = ReplicaCapDrops` on every cell. `OtherDrops = 0`. `ReplayUndrained = 0`.

Reduction is `1 - Forwarded_Krep2 / Forwarded_NoSuppression`. Within each overlap the offered count is identical, so the macro mean equals the pooled ratio.

| Seed | Overlap | Offered | No suppression | Krep=2 | Cap drops | Reduction | Suppressed owner–prefix pairs | Mean visible owners | Prefixes with >2 owners |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026101901 | 0.00 | 48 | 48 | 41 | 7 | 0.145833 | 5 | 1.286 | 3 |
| 2026101902 | 0.00 | 48 | 48 | 46 | 2 | 0.041667 | 1 | 1.192 | 1 |
| 2026101903 | 0.00 | 48 | 48 | 48 | 0 | 0.000000 | 0 | 1.190 | 0 |
| 2026101901 | 0.25 | 80 | 80 | 53 | 27 | 0.337500 | 18 | 1.545 | 10 |
| 2026101902 | 0.25 | 80 | 80 | 56 | 24 | 0.300000 | 16 | 1.414 | 8 |
| 2026101903 | 0.25 | 80 | 80 | 55 | 25 | 0.312500 | 16 | 1.375 | 8 |
| 2026101901 | 0.50 | 112 | 112 | 67 | 45 | 0.401786 | 34 | 1.690 | 18 |
| 2026101902 | 0.50 | 112 | 112 | 69 | 43 | 0.383929 | 32 | 1.576 | 16 |
| 2026101903 | 0.50 | 112 | 112 | 68 | 44 | 0.392857 | 32 | 1.613 | 16 |
| 2026101901 | 0.75 | 144 | 144 | 80 | 64 | 0.444444 | 51 | 1.750 | 26 |
| 2026101902 | 0.75 | 144 | 144 | 82 | 62 | 0.430556 | 48 | 1.692 | 24 |
| 2026101903 | 0.75 | 144 | 144 | 85 | 59 | 0.409722 | 48 | 1.711 | 24 |

Offered volume follows `48 + 4 * round(32 * overlap)`: 48, 80, 112, 144. It was not equalized.

Cap drops exceed suppressed owner–prefix pairs on every cell that drops anything. A hidden owner can advertise more than once. The two quantities are not interchangeable.

## Aggregate by overlap

Three seeds. Sample SD uses `n - 1`. No significance test.

| Overlap | Mean offered | Mean forwarded, no cap | Mean forwarded, Krep=2 | Mean cap drops | Mean suppressed pairs | Mean visible owners | Mean reduction | Sample SD | Min | Max |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | 48 | 48 | 45.000 | 3.000 | 2.000 | 1.223 | 0.062500 | 0.075115 | 0.000000 | 0.145833 |
| 25% | 80 | 80 | 54.667 | 25.333 | 16.667 | 1.445 | 0.316667 | 0.019094 | 0.300000 | 0.337500 |
| 50% | 112 | 112 | 68.000 | 44.000 | 32.667 | 1.626 | 0.392857 | 0.008929 | 0.383929 | 0.401786 |
| 75% | 144 | 144 | 82.333 | 61.667 | 49.000 | 1.718 | 0.428241 | 0.017476 | 0.409722 | 0.444444 |

Mean steps of the reduction: +25.4, +7.6, and +3.5 percentage points. In this matrix each of the three seeds increases at every step. That is an observation of these 12 cells. It is not a general monotone law, and no row was edited to produce it.

Payload bytes, when cited, are `forwarded * 64` (`f6_c_per_arm.csv`). `forwarded * 104` is the existing wire-byte accounting convention, not a measured TCP length.

## Checks the matrix was required to allow

- 0% overlap is not zero suppression. Seeds drop 7, 2, and 0 advertisements. The zero cell is kept.
- A suppressed owner is not always worker 2 or 3. Counting each hidden owner once per prefix, across all 12 cells: worker 1 is hidden on 123 prefixes, worker 2 on 118, worker 3 on 58, and worker 0 on 2. Seed `2026101901` at 0% hides workers 0, 2, and 3.
- Kept owners differ by seed. Among prefixes with at least three owners, the first two arrivals are `(0, 1)` for only a minority (for example 2/18, 1/16, and 0/16 at 50%).
- Pooled first-seen owner order for prefixes with at least three owners: `(0,3,2,1)` 51, `(0,3,1,2)` 38, `(0,2,1,3)` 26, `(0,1,2,3)` 24, then smaller counts including `(2,1,0)` and `(3,1,0,2)`. This pool describes the traces. It is not an extra sample size.
- Peak relay queue on the first cell was `maxq = 3`, and the queue was empty at the stats snapshot. Later cells also finished with `queued = 0`.

## What the numbers can support

On this workload, raising phase-0 overlap from 0 to 0.75 raises both the offered advertisement count (48 to 144) and the fraction removed by a frozen `Krep=2` replay (mean 6.25% to 42.8%). The cap also removes owner visibility: mean visible owners under `Krep=2` stay near 1.2–1.7 while the no-cap owner–prefix count grows with overlap.

The `Krep=2` forwarded counts are paired counterfactual replays of the live FullSync stream. They are not measured end-to-end serving performance with replica suppression enabled on the four GPUs.

Candidate figures, not paper replacements: `fig6c_candidate_reduction.pdf` (reduction versus overlap, min–max across the three seeds, offered count annotated) and `fig6c_candidate_forwarded.pdf` (absolute forwarded counts). Absolute forwarded counts must not be compared across overlap without the offered volume.

Rows: `f6_c_per_cell.csv` (12), `f6_c_per_arm.csv` (24), `f6_c_summary.csv`. Raw frames and relay stats: `raw/`.
