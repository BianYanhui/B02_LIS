# F6-A2 visibility

## Definition, fixed in the replay script

For each generated version, visibility delay is the replay-clock time from its shifted generation until the dispatcher applies some frame for the same owner and lineage whose coverage is at least that version's coverage. A later version can cover an earlier one. Versions that never reach that state are not given delay 0. None of those occurred.

`Dispatcher.apply_upsert` is the frozen method. It overwrites coverage. A lower coverage applied after a higher one would be a regression. Regressions are 0 on every arm. Final dispatcher coverage matches the worker's latest coverage on all 8 feasibility lineages and all 48 formal lineages, both merges, both conditions.

Time-weighted coverage staleness is the integral of `max(0, worker_coverage - dispatcher_coverage)` divided by lineages × replay duration. Worker coverage steps up at generation and does not fall. Dispatcher coverage follows applies.

Socket send time is the relay's `gateway_send_time`. Dequeue is the same release step immediately before that write. There is no separate dequeue timestamp.

## Formal result

P95 over all 48 versions in a seed. Means below are the mean of the three seeds.

| Condition | Merge | P95 delay | Time-weighted staleness | Never visible |
| --- | --- | ---: | ---: | ---: |
| Natural | off | 0.0026, 0.0024, 0.0026 s | 0.60, 0.58, 0.64 tokens | 0 |
| Natural | on | 0.0031, 0.0027, 0.0027 s | 0.67, 0.62, 0.63 tokens | 0 |
| Controlled | off | 16.969, 16.860, 16.689 s | 1392, 1383, 1372 tokens | 0 |
| Controlled | on | 4.031, 4.111, 4.123 s | 692, 675, 657 tokens | 0 |

Natural on and off differ by about 1 ms. That is not a freshness effect.

Controlled merge-on cuts P95 delay from a seed-mean of 16.84 s to 4.09 s, and staleness from about 1382 tokens to about 675 tokens. The same arm forwards fewer frames. Both numbers are `TIMED_REPLAY` under the frozen ACK window.

Per version, formal controlled, 48 versions pooled:

| Version | Merge off median | Merge on median | Merge on frames that were themselves forwarded |
| --- | ---: | ---: | ---: |
| 1024 | 7.64 s | 3.09 s | 6 / 48 |
| 2048 | 9.39 s | 2.48 s | 11 / 48 |
| 4096 | 10.23 s | 1.36 s | 48 / 48 |

Versions 1 and 2 are often not forwarded. They become visible when a later coverage arrives. That later apply is still earlier than the FIFO delivery of the original frame. This run does not show an intermediate version becoming visible later under merge-on than under merge-off.

No TTFT was measured. No routing request was sent during replay.
