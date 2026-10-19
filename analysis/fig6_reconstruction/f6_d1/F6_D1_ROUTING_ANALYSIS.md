# F6-D1 routing

`choose` was not modified. Each world is a new dispatcher with `rr = 0` and loads copied from the confirmed running gauges. The request was sent to the worker `choose` returned. The read-only mirror matched that worker on all 30 trials.

Admission is `CONTROLLED_ADMISSION_ORDER`. Visible owners are sent first through the frozen relay. Full view uses `dedup = 0` and forwards 4. Krep=2 uses `dedup = 2`, forwards the two visible owners, and drops the two hidden owners with `duplicate_holder`. Relay counters match: `replica_cap` is 0 or 2. This order is not a natural arrival distribution.

Native selection still sees every worker. A dropped advertisement removes coverage from the index. It does not remove the worker from least-loaded selection.

## Load 0

| Scenario | Assignment | Full native → selected | Cap native → selected | Route changed | Cached full / cap |
| --- | --- | --- | --- | --- | --- |
| D1 hidden longer | A, 2 pairs | 0 → 3, override | 0 → 0, no override | yes | 4096 / 1024 |
| D1 hidden longer | B, 2 pairs | 0 → 0, no override | 0 → 3, override | yes | 4096 / 1024 |
| D2 visible longer | A, 2 pairs | 0 → 0 | 0 → 0 | no | 4096 / 4096 |
| D2 visible longer | B, 2 pairs | 0 → 3, override | 0 → 3, override | no | 4096 / 4096 |
| D0 equal | A, 2 pairs | 0 → 0 | 0 → 0 | no | 4096 / 4096 |
| D0 equal | B, 2 pairs | 0 → 0 | 0 → 3, override | yes | 4096 / 4096 |

Assignment B is the case the audit predicted. The cap's native worker is 0. That worker holds 4096 tokens on the engine, but its advertisement was dropped, so the index coverage is 0. The guard sees 4096 or 1024 visible tokens against 0 and overrides onto worker 3. When the hidden worker's long cache is the native choice (D1 B), the override moves the request onto the short visible replica. When both views already see 4096 on worker 3 (D2 B), they select the same worker.

Assignment A D1 is the other path. Native worker 0 is visible and holds only 1024. Full view overrides to worker 3 because 3072 extra tokens are 61.44 ms in the frozen formula, above the 0.5 ms guard. The cap has no longer owner in the index, the pre-guard net is 0, and it stays on worker 0.

D0 assignment B changes the GPU and does not change cached tokens. D2 never changes the selected worker. Both are controls, and both happened as the frozen rule specifies.

## D3, two pairs

Visible workers each held one real 256-token completion. Gauges before the measured request were `[1,1,0,0]` for assignment A and `[0,0,1,1]` for assignment B. Waiting was 0. Loads in `choose` were those gauges, not hand-written numbers.

Assignment A full: native worker 2, which is idle and holds 4096, selected 2, no override. Cap: native is still 2, whose coverage is hidden, and the override selects worker 1, which is busy and holds 1024. Assignment B full stays on native worker 0 (idle, 4096). The cap overrides to worker 3 (busy, 1024).

The held requests are not the measured TTFTs.
