# Next iteration

F6-F1 collected the formal Fig. 6(a) count and the formal Fig. 6(c) matrix. Stop here.

The only decision needed is editorial:

- Fig. 6(a) can stay as a compact pending-supersession bar with the 20.78% same-coverage result, or it can be dropped. Another seed on this workload will not produce extension supersessions. Do not reopen a delay model; the phase-rate HTB replay is already `MODEL_REJECTED`.
- Fig. 6(c) can be plotted from `f6_c_summary.csv` and `fig6c_candidate_reduction.pdf`. Keep the zero-reduction seed. Show offered volume beside the reduction. Do not describe the replay as a live `Krep=2` serving measurement.

Do not start Fig. 6(b), Fig. 6(d), TTFT pairing, invalidation churn, pressure-feedback ablation, or a larger worker replay unless a later request says so.

Do not restart `b02-gateway4t4` or retune its HTB to obtain gateway JSON timestamps. The formal cells already match relay receive, forward, and drop counters. A future timestamped log would be a new capture setup, not a repair of these 12 cells.
