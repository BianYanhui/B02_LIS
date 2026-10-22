# F6-D1b coverage-dominance replica retention

Case: the Krep=2 holder set prefers strictly larger coverage. This is a new policy. It does not replace the arrival-order measurement in `f6_d1/`.

## Rule

While fewer than Krep holders are retained, arrival order still fills the set. Once it is full, a new owner is kept only when its coverage is strictly larger than the weakest holder. The weakest holder is the lowest coverage, then the smaller instance id. Equal coverage does not replace anyone. A displaced owner that was already forwarded is hidden with a tombstone queued ahead of the new upsert. `Dispatcher.choose`, `apply_upsert`, the guard, and the cost model are unchanged.

## Protocol

Same physical setup and controlled admission order as `f6_d1`: visible owners advertised first, hidden owners second. Prompt 4128, prefix 4096, suffix 32. Client TTFT is the streamed POST until the first non-empty completion text. First piece on every trial: `unt`. Server TTFT: NOT_MEASURED.

Pilot seed 2026102009, D1, assignment A, excluded from the formal means. Formal seeds 2026102010–13, scenarios D0/D1/D2, both views. D3 is the same coverage pattern as D1 with load 1 on the visible workers, seeds 2026102010–11 only. 30/30 trials valid. Admission label: CONTROLLED_ADMISSION_ORDER.

The run executed the working tree on branch `f6-d1b-coverage-dominance`. `run_status.json` records git HEAD `8d4df0b`, which does not contain this retention change.

## Formal D1 (the changed case)

Hidden owners hold 4096. Visible owners hold 1024 and are advertised first. Cap index after dominance: assignment A `0,0,4096,4096`; assignment B `4096,4096,0,0`. Both views select the same 4096 owner. Relay stat on every cap trial: forwarded 6 (4 upserts + 2 tombstones), replica_cap 0. Tombstone reason `coverage_dominated`: 14 enqueue events, all on the 7 D1/D3 cap cells.

| Quantity | Value | Label |
| --- | --- | --- |
| Pairs | 4/4 valid | OBSERVED |
| Mean ΔTTFT (cap − full) | +1.57 ms | MEASURED_CLIENT_TTFT |
| Median | +0.51 ms | MEASURED_CLIENT_TTFT |
| Sample SD | 2.83 ms | MEASURED_CLIENT_TTFT |
| Range | −0.49 to +5.76 ms | MEASURED_CLIENT_TTFT |
| Coverage loss | 0 on every pair | OBSERVED (`cached_tokens` 4096 vs 4096) |
| Route change | 0/4 | OBSERVED |
| Model regret | 0.00 ms | MODEL_DERIVED |

Assignment A mean Δ +3.20 ms (both views select worker 3). Assignment B mean Δ −0.05 ms (both views select worker 0).

Prior arrival-order result, different seeds and a different day: mean Δ +1812.59 ms, coverage loss 3072, route change 4/4. That number stays the measurement of the old rule. It is not subtracted from the paper's 118–391 ms range.

## Controls

D0, all four owners at 4096. Equal coverage, so the cap still keeps the first two. Route change 2/4, both assignment B (full worker 0, cap worker 3). Coverage loss 0. Mean Δ −325.17 ms is the first formal pair only (seed 2026102010, full then cap, 1435.5 vs 129.6 ms, same worker, both cached 4096). The other three deltas are +5.29, −1.30, +1.19 ms. Median Δ −0.05 ms.

D2, visible 4096 then hidden 1024. The later coverage is smaller, so it is dropped. Cap index is the two visible owners. Route change 0/4. Coverage loss 0. Mean Δ −0.30 ms, median −0.87 ms, range −6.95 to +7.48 ms.

D3, n=2, held load on the visible workers. Dominance keeps the idle 4096 owners, so both views select that owner. Coverage loss 0, route change 0/2, model regret 0.00 ms. The mean Δ −654.41 ms is the first pair (1424.5 vs 115.8 ms, same worker 2, both cached 4096). The second pair is −0.10 ms. Not a headline.

Model cost remains `T = load*2 + max(0, prompt−warmup_coverage)/50`. On D1 both views have warmup coverage 4096 and load 0, so T is 0.64 ms on each side. Regret is not a TTFT.

## Gates

Pilot D1 assignment A: residency `1024,1024,4096,4096` on both views, cap index `0,0,4096,4096`, both selected warmup coverages 4096, both TTFTs measured, cache rebuilt. `feasibility_gates.json` stop is false.
