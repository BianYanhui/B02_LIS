# F6-P1 routing counterfactual

Both views are applied to one frozen snapshot. They do not come from two live request sequences.

## Rule that was not changed

Upserts are enqueued in instance order 0, 1, 2, 3. `Relay._admit_replica` with `dedup=2` keeps the first two holders. Load and coverage are not inputs. On every snapshot the cap forwards 2 advertisements and drops 2. Visible owners are 0 and 1. Suppressed owners are 2 and 3.

`dedup=0` on the same function forwards all four. Merge, priority, adaptive admission, and global top-k are off in both views, so the only difference is the cap. That matches the F6-P0 smoke. It is not the published StaticSemantic bundle.

Routing is frozen `Dispatcher.choose` (`j=4`, 50 prefill tokens/ms, 2 ms per in-flight request, 0.5 ms guard). `choose` is not argmin of `T`. It picks the least-loaded worker among all four, then affinity-overrides only when the coverage gain beats the guard. The least-loaded scan includes workers the cap has hidden, because load is a local counter, not an advertisement.

## Cost

`T_i = load_i * 2 ms + max(0, 1083 - cached_i) / 50`.

`1083` is the probe’s measured prompt length. `cached_i` is that worker’s measured `vllm_cached_tokens` (1056 on every worker). The prefill term is `27/50 = 0.54 ms` on every owner. It cancels in the regret. Label: **MODEL-DERIVED**. Not TTFT.

`Regret = T(selected_cap) - T(selected_full)` on that same snapshot. Separately, the full view’s selected worker was inside the model argmin set on all 24 view rows (`full_is_model_argmin = 1`). The cap’s selected worker leaves that set only when regret is 2 ms.

## Balanced, harness `[0,0,0,0]`

Every owner has `T = 0.54 ms`. Regret is 0 on all four round-robin positions.

The selected index still changes when the cursor lands on a hidden owner. At `rr=2` the full view returns worker 2. The cap cannot see worker 2, the guard sees a 1056-token gain (`expected_net_ms = 21.12`), and `choose` returns worker 1. At `rr=3` the full view returns worker 3 and the cap returns worker 0. Both chosen workers have the same `T`. A changed index is not regret.

`best_owner_hidden` stays 0: every worker is an argmin, and two of them remain visible.

## Imbalanced

| Snapshot | Harness load | Engine agrees | Full select | Cap select | Best hidden | Regret (ms) |
| --- | --- | --- | ---: | ---: | --- | ---: |
| B0 | 1,0,0,0 | no | 1 | 1 | no | 0 |
| B1 | 1,1,0,0 | yes | 2 | 1 | yes | 2.0 |
| B2 | 1,1,1,0 | yes | 3 | 1 | yes | 2.0 |
| B3 | 1,1,1,1 | yes | 0 | 0 | no | 0 |
| B4 | 2,1,1,1 | no | 1 | 1 | no | 0 |
| B5 | 2,2,1,1 | no | 2 | 1 | yes | 2.0 |
| B6 | 2,2,2,1 | no | 3 | 1 | yes | 2.0 |
| B7 | 2,2,2,2 | no | 0 | 0 | no | 0 |

B1: workers 2 and 3 are the only argmin (`T = 0.54`). Both are hidden. The cap affinity-overrides onto worker 1 (`T = 2.54`, `expected_net_ms = 19.12`). Regret is one queue penalty. The prefill terms are equal.

B2: the unique argmin is worker 3. The cap selects worker 1. Same 2.0 ms.

B5 and B6 also show 2.0 ms on the harness counter, but `num_requests_running` was `[1,1,1,1]` at those timestamps. Those two regrets are not engine-confirmed. B0, B4, and B7 are not either; their harness regret is already 0.

No counterfactual request was executed. `actual_executed_worker` and `actual_ttft_ms` are empty. The probe and reprobe TTFTs are measurements of requests that were sent to every worker, not the TTFT of the worker the other view would have chosen.

Table: `analysis/fig6_reconstruction/f6_p1/counterfactual_routing.csv`.
