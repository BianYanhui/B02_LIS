# F6-B1 causal timeline

Two clocks are involved. The reset and the native clear are on the capture clock. Gateway enqueue, forward, and dispatcher apply are on the replay clock. The stale-positive duration used below is the sum of the observed detection piece and the replay apply delay. It is a constructed duration. It is not one stopwatch running through a live gateway during the GPU reset.

## Segments

Priority can change only the queue wait. The other pieces are shared, or are local socket time.

| Segment | Where it is measured | Formal result |
| --- | --- | --- |
| Reset command → HTTP done | Capture, worker 0 | 4.45–5.87 ms across four seeds |
| HTTP done → native `AllBlocksCleared` | Capture tap | 4.68–5.32 ms. This is the detection term inside `D_stale` |
| Native clear → tombstone stamp | Adapter | 0 by construction. The stamp is the clear receive time |
| Tombstone send → gateway enqueue | Replay | Included in the sub-millisecond send path; the wait that matters is below |
| Enqueue → forward | Replay, `queue_wait_s` | The priority effect. Formal means: FIFO 5.584 s, priority 0.029 s |
| Forward → apply | Replay | 0.25–0.33 ms on the formal controlled cells |

`D_tomb` in the per-cell table is `apply_delay_s` (replay apply minus replay tombstone send). `D_stale` is that delay plus the capture's HTTP-done-to-clear detection. Empty samples are not written as 0. Every primary cell has `delay_status = MEASURED`.

## Controlled backpressure, formal seeds

ACK hold 0.7922039031982422 s, `max_inflight = 1`, frozen from the pilot before these seeds. Probe offset 2.371638536453247 s after enqueue, also frozen from the pilot midpoint of the two apply delays.

| Seed | Policy | Pending at enqueue | Dequeue position | Queue wait | Constructed stale duration | `choose` at the probe | Tombstone already applied |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026101920 | FIFO | 7 | 7 | 5.573 s | 5.578 s | worker 0 | no |
| 2026101920 | Priority | 7 | 0 | 0.017 s | 0.023 s | worker 1 | yes |
| 2026101921 | FIFO | 7 | 7 | 5.573 s | 5.578 s | worker 0 | no |
| 2026101921 | Priority | 7 | 0 | 0.017 s | 0.022 s | worker 1 | yes |
| 2026101922 | FIFO | 7 | 7 | 5.607 s | 5.612 s | worker 0 | no |
| 2026101922 | Priority | 7 | 0 | 0.052 s | 0.058 s | worker 1 | yes |

Position 0 means the tombstone was the next frame forwarded after it was enqueued. Position 7 means seven upserts already in the queue left before it. Priority does not pull back a frame that has already been dequeued. The formal priority waits, 17–52 ms, are the remainder of that one in-flight frame. The pilot's priority wait was 0.786 s, one full ACK hold. Both are inside the non-preemptive bound of one inflight frame.

The formal pending depth is 7 rather than the pilot's 4 because the formal capture span from the first background to the clear was about 1.55 s, against the pilot's 3.17 s, while the ACK hold stayed at the pilot value. More of the eight upserts were still unsent. The probe offset was not moved to follow that.

## Natural delivery

`max_inflight = 0`, no ACK hold. Pending at enqueue was 0 in every natural cell. Dequeue position was 0 for FIFO and for priority. Queue wait was 0.21–0.29 ms. Both policies had applied the tombstone before the same 2.372 s probe, and both selected worker 1. This is the negative control.

## Dispatcher state at the decision

Loads were 0. With both owners advertised at 4096, frozen `choose` returns worker 0 and does not override: native coverage is already 4096, so the guard has nothing to gain by leaving. After `apply_tombstone(0, X)`, the same call returns worker 1 with the override set. A counterfactual dispatcher with worker 0's entry removed also returns worker 1. The stale entry is what keeps the request on worker 0.

Worker 1's advertisement is still present in every one of those decisions.
