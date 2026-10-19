# F6-B1 priority comparison

The comparison is FIFO (`priority = 0`) against invalidation priority (`priority = 1`). Merge, dedup, adaptive utility, and coverage top-k are off in both. Rate limiting is off. Each formal seed replays one captured event stream. The controlled arm adds the same frozen ACK hold to both policies. The natural arm adds nothing. The unit is the seed. The two timing replays that chose the probe offset are `probe_is_frozen = 0` and are not in these means.

`D_stale` is constructed: capture detection (HTTP done → native clear) plus replay apply delay. Priority does not change the detection term. The paired difference is therefore the change in replay apply delay, which is the change in queue wait.

## Formal seeds, n = 3

No cell was dropped. All 12 formal relay cells applied the tombstone. There is no empty-sample percentile.

### Natural

|  | FIFO | Priority |
| --- | ---: | ---: |
| Mean constructed stale duration | 0.007981 s | 0.008789 s |
| Sample SD | 0.000717 s | 0.001369 s |
| Min, max | 0.007266, 0.008700 s | 0.007384, 0.010120 s |
| Mean queue wait | 0.000252 s | 0.000242 s |
| Mean pending at enqueue | 0 | 0 |
| Decision-level causal invalid routes | 0 / 3 | 0 / 3 |

Paired difference, priority minus FIFO, on stale duration: mean 0.000808 s, sample SD 0.001156 s, range 0.000118–0.002142 s.

### Controlled backpressure

ACK hold 0.7922039031982422 s. `max_inflight = 1`. Frozen with zero adjustments.

|  | FIFO | Priority |
| --- | ---: | ---: |
| Mean constructed stale duration | 5.589294 s | 0.034055 s |
| Sample SD | 0.019448 s | 0.020344 s |
| Min, max | 5.577788, 5.611749 s | 0.021870, 0.057540 s |
| Mean queue wait | 5.584111 s | 0.028891 s |
| Mean apply delay | 5.584418 s | 0.029178 s |
| Mean pending at enqueue | 7 | 7 |
| Dequeue position | 7, 7, 7 | 0, 0, 0 |
| Decision-level causal invalid routes | 3 / 3 | 0 / 3 |

Paired difference, priority minus FIFO, on stale duration: −5.555592, −5.555918, −5.554208 s. Mean −5.555239 s, sample SD 0.000908 s. The same three differences on queue wait have mean −5.555220 s. Three seeds are not a significance claim. The SD is small because every formal seed still had seven upserts queued and the ACK hold was fixed, so FIFO's wait is seven holds plus the remainder of the one frame already in flight.

## Pilot, not in the formal mean

Seed `2026101919`, same frozen offset. Natural: both policies, pending 0, stale duration 0.006565 s and 0.007054 s, both select worker 1. Controlled: pending 4, FIFO position 4 and stale duration 3.964682 s, selecting worker 0; priority position 0 and stale duration 0.791104 s, selecting worker 1. The timing pass that froze the offset, at a temporary probe of one ACK hold, showed the same split and is stored with `probe_is_frozen = 0`.

## Boundary

The controlled arm is an application ACK window on an otherwise local relay. It is not a 10 Mbit/s link, not the F6-A2 hold of 0.588 s, and not the natural four-GPU control path. F6-P0's offline 8-versus-0 slot result is not this measurement. Natural delivery is the negative control: once the queue is empty, priority has nothing left to reorder.
