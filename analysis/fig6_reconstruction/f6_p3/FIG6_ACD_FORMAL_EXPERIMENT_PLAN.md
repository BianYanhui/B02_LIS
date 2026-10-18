# Formal experiment plan for Fig. 6(a)(c)(d)

This plan is for the next round. Do not start it from this readiness run. The F6-P3 smoke, the calibration overlap logs, and the rejected HTB replay are not paper points.

Fig. 6(b) stays closed. Do not retune `b02-gateway4t4`. Do not use `experiments/4t4/restart_4t4.sh` (it truncates `analysis/formal4t4/server_logs`). New vLLM logs go under a new analysis directory.

## Fig. 6(a) — pending supersession, not extension delay

### Hypothesis

While a prior advertisement for the same `(worker, digest)` is still unsent, replacing it reduces the number of state updates the gateway forwards. In the recorded reuse workload those replacements do not increase coverage.

### Why the axes change

Extension count is zero on every reconstructable log, so extension frequency is not the x-axis. The HTB replay missed the observed queue (21 versus 74 supersessions; 5.09 s versus 41.42 s), so P95 visibility is not the y-axis of the minimal figure.

- X: one recorded operating point, the dynamic `reuse_intensive` LatestOnly cell (nominal rho 0.5, with the phase rate changes already in that cell). Label it as that cell, not as a production bottleneck and not as 10 Mbit/s.
- Y: forwarded state updates.
- Supporting: duplicate-supersession ratio, extension ratio (expected 0), offered count. Merge-on P95 latest-state visibility may be plotted as a one-arm annotation. No merge-off visibility bar.

### Minimal matrix (submittable)

| Item | Setting |
| --- | --- |
| Arms | LatestOnly (merge on; priority, cap, adaptive, top-k off) and the merge-off identity |
| Workload | The five already recorded dynamic gateway logs, reps 0–4 |
| Seeds | Those five repetitions. No new seed |
| Cells | 10 reported rows (5 observed, 5 identities). New runs: 0 |
| Runtime | Under 10 minutes of CPU aggregation. No GPU |
| Comparison | Paired within rep: forwarded_on versus offered. Report mean, min, and max across the five reps. No significance test |

Merge-off forwarded equals offered only because each of these logs balances (`forwarded + superseded = offered`) and LatestOnly has no other drop. Recompute that balance per rep. If a rep does not balance, drop the identity for that rep and mark it `NOT_BALANCED`.

### Paper claim

Same-coverage supersession removes pending duplicate advertisements. Across the five recorded reps the removed fraction is about one fifth of offered updates (rep 0 is 74/384).

### If the result is zero or negative

A zero gap means the prior advertisement had already been forwarded. Say the cell had no pending duplicate, and do not describe it as an extension failure. A negative gap cannot occur under the identity; if a recomputation shows one, the log balance is wrong and the cell is excluded.

### Enhanced matrix (not authorized until a gate passes)

Do not run a visibility sweep yet. Gate, pre-registered on dynamic LatestOnly rep 0: a merge-on replay must fall within 10% of 74 supersessions and within 20% of 41.42 s P95 latest-state visibility. The phase-rate HTB model already failed this gate. If a later service model passes, the sweep is 5 traces × {merge off, merge on} × 3 service rates = 30 CPU cells, well under an hour, and visibility becomes eligible. If the gate fails, the figure stays the minimal count comparison.

A controlled `original_compatible` compression can be a separate appendix demonstration. It must be labeled `CONTROLLED`, must not be called a vLLM extension frequency, and must keep coverage-decreasing step wraps out of the extension numerator. It is not part of the minimal figure.

## Fig. 6(c) — replica-cap efficiency

### Hypothesis

Once prefixes have more than two admitted owners, Krep=2 reduces forwarded advertisements relative to the same offered stream with the cap off. The reduction tracks how many prefixes exceed two owners, not the overlap parameter by itself.

### Configuration

Pool 32, four workers, 48 requests, warmup 12, `reuse_intensive`, output length 4. Record one FullSync offered stream per cell with merge, priority, adaptive, and top-k off. Replay that exact stream through the frozen relay at dedup 0 and dedup 2. Drain both arms. Log live undrained frames separately; do not add them to cap drops.

Overlap definition stays `round(pool_size * overlap)` phase-0 lineages, each published from all four workers. Record gateway arrival order. Do not assume the kept owners are 0 and 1.

### Axes and baselines

- X: overlap at 0, 0.25, 0.5, 0.75.
- Y: forwarded state updates, with offered count drawn for the same points. The text reports `reduction = 1 - forwarded_krep2 / forwarded_no_suppression`.
- Arms: no suppression, Krep=2.
- If offered counts differ, which they will, the caption states the offered count at each x position. Do not compare raw forwarded totals across overlap as if the input size were fixed.

### Minimal matrix

| Item | Setting |
| --- | --- |
| Factors | 4 overlap levels × 2 arms |
| Seeds | `2026101901`, `2026101902`, `2026101903`. Do not reuse `2026072640` |
| Live cells | 12 FullSync recordings (the Krep=2 arm is an offline replay of each recording) |
| Reported cells | 24 |
| Runtime | About 30 minutes on one already-loaded four-T4 boot, plus seconds of CPU. Calibration cells of this size were on the order of one to two minutes each |
| Comparison | Paired within seed, because both arms share the offered stream. Mean and min/max of the reduction across three seeds. No significance test |

### Enhanced matrix

Add seeds `2026101904` and `2026101905`. Live cells 20, reported cells 40, about 45 minutes on one boot.

### Paper claim

Raising the fraction of prefixes placed on all four workers increases the advertisements Krep=2 removes, after the offered volume is taken into account. At the readiness seed the full-drain reductions were 6.25%, 41.07%, and 43.06% at 0%, 50%, and 75%. Those three numbers are the pilot, not the figure.

### If the result is zero or negative

Inspect the multiplicity histogram before changing the cap. A near-zero reduction means fewer than three owners arrived for almost every prefix, including at a high overlap setting if the extra publishes were from owners already kept. A negative reduction would mean the two arms did not see the same stream or another drop was left on. Do not explain it as a routing-latency change. This figure does not measure TTFT.

## Fig. 6(d) — model routing cost of the cap

### Hypothesis

With equal prefix coverage, model regret is one queue penalty (2 ms) times the load gap when the lighter owner is hidden, and it is 0 when a visible owner is at least as light. This is `T(cap_selected) - T(full_selected)` from `Dispatcher.choose` on engine-confirmed loads. It is not executed TTFT.

### Configuration

Same model and prefix recipe as F6-P3. New salts per seed. Admission order fixed at 0, 1, 2, 3 so the hidden owners stay 2 and 3. One 640-token holder per assigned worker. Confirm running gauges before sampling. Four samples at 0.50 s; the hold counts as one observation only if all four match the target and are `ENGINE_CONFIRMED`. If a hold misses the target for 5 s, record the failure and do not add requests.

Do not run a TTFT pair. The pairing conditions in `F6_D_HELD_LOAD_REPORT.md` are still missing.

### Axes

- X: hidden-owner load advantage, DeltaLoad in `{-1, 0, +1}`. Not overlap. Fig. 6(c) answers a different cause.
- Y: model-based routing regret (ms).
- Supporting, from the same snapshots: forwarded ads (expected 2 of 4), visible owners, coverage, confirmation. Cite the Fig. 6(c) reduction in the caption as the signaling side of the trade-off. Do not put overlap on this x-axis.

Target vectors: `[0,0,0,0]`, `[1,1,0,0]`, `[0,0,1,1]`.

### Minimal matrix

| Item | Setting |
| --- | --- |
| Levels | 3 |
| Arms | Full view and Krep=2 on the same snapshot |
| Seeds | 3, 4, 5. Seed 1 is the F6-P3 pilot and is not a paper seed |
| Cells | 9 holds |
| Runtime | About 5 minutes of measurement after the model is loaded, plus about 1 minute to load. One boot |
| Comparison | Mean regret at each DeltaLoad. A hold that is not fully confirmed is listed and excluded from the mean. No significance test |

### Enhanced matrix

Add DeltaLoad `+2` with vector `[2,2,0,0]` (two holders on workers 0 and 1, none on 2 and 3), and add seeds 6 and 7. Cells: 5 seeds × 4 levels = 20 holds. About 12 minutes of measurement. If `[2,2,0,0]` does not confirm, keep the failure and do not retune the token count.

### Paper claim

Hiding two of four replica owners costs a model-estimated queue penalty when a hidden owner is strictly lighter, and costs no model penalty when the visible owners are lighter. Under the pilot's equal 1056-token coverage the penalty was exactly 2.0 ms at DeltaLoad `+1` and 0 at `-1`.

### If the result is zero or negative

Zero regret at DeltaLoad `+1` means the cap did not hide the lighter owner or the coverages were not equal. Report the coverages and the selected workers. Positive regret at DeltaLoad `<= 0` contradicts the equal-coverage account; do not average it away and do not convert it into a TTFT story. A failed hold stays in the log as a failed hold.

## Unified matrix

| Experiment | Factor levels | Baselines | Seeds | Cells | Estimated runtime | Readiness |
| --- | --- | --- | ---: | ---: | --- | --- |
| Fig. 6(a) minimal | 1 recorded dynamic operating point | Merge on (observed) vs merge off (identity) | 5 existing reps | 10 reported, 0 new | <10 min CPU, 0 GPU | `READY_WITH_LIMITATIONS` |
| Fig. 6(a) enhanced | 3 service rates, only after the rep-0 clock gate | Merge off vs merge on | 5 traces | 30 CPU | <1 h CPU, 0 GPU, gated | gate not passed |
| Fig. 6(c) minimal | Overlap 0, 0.25, 0.5, 0.75 | No suppression vs Krep=2 | 3 new | 12 live + 12 CPU = 24 | ~30 min, one 4-GPU boot | `READY_WITH_LIMITATIONS` |
| Fig. 6(c) enhanced | Same 4 overlaps | Same | 5 new | 20 live + 20 CPU = 40 | ~45 min, one boot | same |
| Fig. 6(d) minimal | DeltaLoad −1, 0, +1 | Full view vs Krep=2, same snapshot | 3 new | 9 holds | ~5 min measure + ~1 min load | `READY-MODEL` |
| Fig. 6(d) enhanced | DeltaLoad −1, 0, +1, +2 | Same | 5 new | 20 holds | ~12 min measure | `READY-MODEL` |

Recommended package for a six-page paper: the three minimal matrices. That is 10 analysis rows, 12 new live cells, and 9 holds. GPU time is about 35 minutes plus one model load. The enhanced rows are optional.

Run (c) first, then (d), on separate boots so a gateway recording and a held-load prefix do not share a process. Assemble (a) from the existing logs in parallel; it does not need the GPU. Do not start the (a) visibility sweep unless the clock gate passes.
