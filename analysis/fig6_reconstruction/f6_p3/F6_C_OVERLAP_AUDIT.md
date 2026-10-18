# F6-P3 Fig. 6(c) — Replica overlap and isolated Krep=2

## Definition

In `run_formal4t4.py`, `--overlap` is the fraction of phase-0 lineages physically requested on every vLLM instance before the measured trace:

`overlap_seed_count = round(pool_size * overlap)`

Each seed prefix is requested on all four workers. After those requests return, `publish_state` is called for instances 0, 1, 2, 3. The harness send order is 0 then 1 then 2 then 3. The gateway has one reader per agent connection, so the arrival order in the log is not that send order.

Overlap is not the fraction of repeated advertisements. A prefix outside the seed set is advertised by whichever worker served a later request. Krep=2 drops a new owner only after two distinct owners of that digest have already been admitted. Another advertisement from an owner already inside the cap is kept. Load and coverage are not inputs.

The calibration cells used `pool_size = 32`, four workers, `n_requests = 48`, warmup 12, `reuse_intensive`, seed `2026072640`, one repetition. Evidence type in the cells CSV is `hybrid_live_vllm_real_kernel_link`. This audit replays those logs. It does not promote them to a paper figure. The old overlap sweep remains not-for-paper.

| Overlap | Seed prefixes | Seed requests | FullSync upserts | Live FullSync forwarded | Live undrained at cell end |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | 0 | 0 | 48 | 48 | 0 |
| 25% | 8 | 32 | 80 | 80 | 0 |
| 50% | 16 | 64 | 112 | 112 | 0 |
| 75% | 24 | 96 | 144 | 123 | 21 |

Offered volume is `48 + 4 * seed_prefixes`. The 25% row is the recorded calibration cell; it was not one of the six pairing replays. The extra 21 frames at 75% were enqueued by FullSync and never forwarded. FullSync has no replica cap, so those 21 are window-end backlog. They are not cap savings.

## Arrival order

Among prefixes that accumulated at least three owners, the first-seen owner order in the FullSync log is rarely `(0, 1, 2, 3)`. At 50% the common prefixes of the order are `(0, 3, 2, 1)`, `(0, 3, 1, 2)`, and `(0, 2, 1, 3)`. Krep=2 therefore keeps whichever two owners arrive first, not workers 0 and 1 by identity. The offline admission order used in F6-P1/P2 is a separate, serialized schedule.

## Old calibration drops are not an isolated cap

Adaptive on the same calibration files mixes mechanisms. At 50% overlap its cells row is 112 upserts, 51 forwarded, 36 `duplicate_holder`, 5 superseded, 20 low-utility. Those 36 holder drops are not a pure Krep=2 effect. FullSync on the same files has zero holder drops, zero supersessions, and zero low-utility drops. The pairing below uses the FullSync offered stream only.

## Six-arm replay

One seed (`2026072640`). Same FullSync enqueue sequence for both arms. Merge, priority, adaptive utility, and top-k are off. The relay is the frozen `Relay._admit_replica`. Both arms are then drained completely. `Reduction = 1 - forwarded_krep2 / forwarded_no_suppression` on that full drain. Cap drops are counted separately from other drops and from window-end backlog.

| Overlap | Arm | Offered | Forwarded after full drain | Cap drops | Other drops | Window-end unforwarded | Reduction | Prefixes with more than 2 owners | Mean owners kept | Missing owner pairs |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | none | 48 | 48 | 0 | 0 | 0 |  | 2 | 1.58 | 0 |
| 0% | Krep=2 | 48 | 45 | 3 | 0 | 0 | 6.25% | 2 | 1.42 | 3 |
| 50% | none | 112 | 112 | 0 | 0 | 0 |  | 19 | 3.15 | 0 |
| 50% | Krep=2 | 112 | 66 | 46 | 0 | 0 | 41.07% | 19 | 1.81 | 36 |
| 75% | none | 144 | 144 | 0 | 0 | 0 |  | 27 | 3.34 | 0 |
| 75% | Krep=2 | 144 | 82 | 62 | 0 | 0 | 43.06% | 27 | 1.86 | 52 |

`forwarded_gap - cap_drops = 0` on every Krep=2 row. No other mechanism and no undrained tail is inside the reduction.

Owner multiplicity of the offered stream:

- 0%: `1:11; 2:6; 3:1; 4:1`
- 50%: `1:5; 2:3; 3:2; 4:17`
- 75%: `1:5; 2:3; 3:2; 4:25`

At 0% the seed count is zero, but routing still puts two prefixes on more than two workers, so Krep=2 drops 3 advertisements. The level is a small effect, not a structural zero.

At 50%, 16 seed prefixes would contribute 32 drops if each of four owners advertised once and the cap kept two. The replay drops 46 advertisements and 36 owner pairs. The extra advertisements are repeated ads from owners already over the cap, plus prefixes the trace itself placed on a third or fourth worker. The same pattern holds at 75% (62 advertisement drops, 52 missing owner pairs).

Metadata bytes in the CSV are `forwarded * 64` payload bytes and `forwarded * 104` using the on-wire accounting convention. The 104-byte figure is not a measured TCP length.

## Consequence for the figure

The cap is isolatable and the reduction grows once many prefixes have more than two owners. Raw forwarded counts are not comparable across overlap levels, because offered counts are 48, 112, and 144. A formal plot has to show offered volume or use the reduction ratio. The 75% live cell's 21 undrained frames must stay out of the cap numerator.

This six-row table is the readiness pairing, not Fig. 6(c).
