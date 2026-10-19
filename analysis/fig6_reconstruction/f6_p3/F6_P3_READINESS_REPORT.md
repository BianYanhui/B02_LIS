# F6-P3 readiness — Fig. 6(a)(c)(d)

Branch `f6-p3-formal-readiness`, parent `f401014`. Fig. 6(b) was not run. No formal matrix was started. The smoke numbers below are not paper points.

## Verdicts

| Figure | Verdict | Reason |
| --- | --- | --- |
| Fig. 6(a) | `READY_WITH_LIMITATIONS` | Extension and duplicate are separable. In every reconstructable gateway log the extensions are zero and the merges are same-coverage duplicates. Forwarded-count reduction is identified. Latest-state visibility under merge off is not, because the HTB replay does not match the log. |
| Fig. 6(c) | `READY_WITH_LIMITATIONS` | Overlap is defined, and Krep=2 is isolatable: the forwarded gap equals the cap drops. Offered counts rise with the preseed, the 0% level barely fires, and the 75% live cell left 21 frames undrained. The reduction can be a formal result only with offered volume shown. |
| Fig. 6(d) | `READY-MODEL` | Both held-load signs were engine-confirmed and repeatable. Model regret is 2.0 ms at DeltaLoad `+1` and 0 at `-1`. `TTFT_PAIRING_NOT_READY`. |

## Fig. 6(a)

OBSERVED on dynamic LatestOnly rep 0: 384 offered, 310 forwarded, 74 superseded, 0 extension, 0 other, 0 anomalous. P95 latest-state visibility 41.42 s. P95 survivor delivery delay 31.97 s. Five dynamic reps agree: extension count is 0, superseded is 70–94.

ACCOUNTING IDENTITY for merge off: 384 forwarded. The log balances and LatestOnly has no other drop. This is the signaling comparison. It is not a timed visibility comparison.

MODEL-REJECTED: phase rates 1754 / 730 / 1754 bit/s produce 21 supersessions and a 5.09 s p95 against the log's 74 and 41.42 s. Those rates are the experiment's controlled HTB settings, not 10 Mbit/s and not a production bottleneck. They are not a usable delay model.

CONTROLLED compression of `original_compatible_trace_rep0` does create 56 coverage-increasing merges and 20 coverage-decreasing wraps. That clock is not a vLLM extension frequency.

The eight-cell means 37.78 / 30.95 / 15.18 stay `NOT_RECONSTRUCTABLE`.

## Fig. 6(c)

CPU replay of the FullSync calibration logs, seed `2026072640`, pool 32, four workers. Full drain, other mechanisms off.

| Overlap | Offered | Forwarded, no cap | Forwarded, Krep=2 | Cap drops | Reduction |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | 48 | 48 | 45 | 3 | 6.25% |
| 50% | 112 | 112 | 66 | 46 | 41.07% |
| 75% | 144 | 144 | 82 | 62 | 43.06% |

Residual against cap drops is 0. Live FullSync at 75% forwarded 123, not 144; the 21-frame gap is window-end backlog and is outside this reduction. Gateway arrival order is not worker 0, 1, 2, 3. Adaptive's old holder-drop column mixes low-utility and supersession.

## Fig. 6(d)

OBSERVED residency: cached tokens 0 at warmup, 1056 at probe and at both reprobes, hit delta +1056, prompt 1083. Engine block hash not measured.

OBSERVED load: four of four samples in each phase matched the target vector on the running gauges. D-positive `[1,1,0,0]`, D-negative `[0,0,1,1]`. The engine returned to idle between them. Measurement 38.3 s. GPUs were released afterward.

MODEL-DERIVED regret: 2.0 ms and 0.0 ms. Prefill term 0.54 ms on every worker. Not TTFT.

NOT_MEASURED: paired executed TTFT. The missing conditions are listed in `F6_D_HELD_LOAD_REPORT.md`.

## Claim each formal figure can carry

- Fig. 6(a) can support a signaling claim about pending same-coverage supersession. It cannot support an extension-frequency or visibility-delay claim until a clock matches the log or both arms are recorded.
- Fig. 6(c) can support a signaling-efficiency claim against overlap, with offered counts or a reduction ratio on the axis.
- Fig. 6(d) can support a model routing-cost claim against hidden-owner load advantage. It cannot support a measured serving-latency claim.

The executable matrices are in `FIG6_ACD_FORMAL_EXPERIMENT_PLAN.md`.
