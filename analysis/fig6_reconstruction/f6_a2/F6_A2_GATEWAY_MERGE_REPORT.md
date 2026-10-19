# F6-A2 gateway merge

## Path

Frozen `Relay` on `127.0.0.1:9720`, dispatcher on `127.0.0.1:9721`. `b02-gateway4t4` on `9710` was left as it was. Priority, replica cap, adaptive admission, and coverage top-k were off. Merge is the only flag that changes.

Both arms read the same captured upserts and the same inter-arrival gaps. The replay clock shifts the first generation to the start of that arm. Delays below are on that clock. They are `TIMED_REPLAY`, not an inline delay measured while vLLM was serving.

F6-P3's HTB replay was not used.

## Capacity

Natural: `max_inflight=0`, ACK as soon as the frame is read. No added delay.

Controlled: `max_inflight=1`. ACK is delayed by `2 / feasibility_offered_rate`. That rate was 3.400 upserts/s on seed `2026101905`, so the delay is 0.588170 s. The rule was written to `capacity_rule.json` before either formal seed was replayed. It was not changed after the formal counts.

Measured on the formal merge-off arms, where every frame is sent: inter-send gap p50 is 0.589 s, and the service rate is 1.697, 1.696, and 1.697 frames/s. Formal offered rates on the same gaps are 4.748, 4.676, and 4.587 upserts/s. Intensity is offered/service ≈ 2.80, 2.76, and 2.70. The feasibility arm was 2.0 by the formula. The formal intensity is higher because those traces were faster and the delay stayed frozen.

This window is not a 10 Mbit path and not the natural four-GPU bottleneck.

## Formal seeds

Seeds `2026101906`, `2026101907`, `2026101908`. 48 offered upserts each. Feasibility seed `2026101905` fixed the delay and is not in these means.

`forwarded + superseded = offered` on every arm. Relay `K_STATS` matches those two counts. Anomalous, same-coverage, tombstone, and other supersessions are 0.

| Condition | Merge | Forwarded | Superseded | Eligible pending extensions | True extensions | Already forwarded before the next version |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Natural | off | 48, 48, 48 | 0 | 0 | 0 | 32, 32, 32 |
| Natural | on | 48, 48, 48 | 0 | 0 | 0 | 32, 32, 32 |
| Controlled | off | 48, 48, 48 | 0 | 0 | 0 | 32, 32, 32 |
| Controlled | on | 21, 22, 22 | 27, 26, 26 | 27, 26, 26 | 27, 26, 26 | 5, 6, 6 |

Each true extension is coverage 1024→2048 (42 of the formal 79) or 2048→4096 (37). The ratio of true extensions to eligible pending opportunities is 1. Eligible means the previous coverage was still in `queued_up`. Increases whose previous frame had already been dequeued are not in that denominator. There are 32 coverage increases per seed (16 lineages × 2). The pending fraction is 27/32, 26/32, and 26/32.

Signaling reduction `1 - forwarded_on/forwarded_off` is 0 on every natural seed. On the controlled seeds it is 0.5625, 0.5417, and 0.5417. Mean 0.5486, sample SD 0.0120.

Natural is `NATURAL_NO_MERGE_OPPORTUNITY`. The localhost relay drains each upsert before the next version of that lineage exists.

Max observed tick backlog on controlled merge-on is 12, 10, and 9 frames. Natural backlog is 0.

## Feasibility, not in the mean

Seed `2026101905`, 24 upserts. Controlled merge-on forwarded 16, superseded 8, all true extensions, reduction 0.333. Natural superseded 0. Gate 3 passed on this controlled arm, which is why the formal seeds ran.
