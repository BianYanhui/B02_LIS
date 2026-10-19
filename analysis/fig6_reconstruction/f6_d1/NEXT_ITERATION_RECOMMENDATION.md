# Next iteration

Do not start it from this handoff unless the user asks.

Fig. 6(d) now has a measured client-TTFT gap for one condition: load 0, controlled admission, hidden owners at 4096 tokens and visible owners at 1024. Four formal pairs, mean Δ 1813 ms, and the equal-coverage and visible-longer controls. The candidate figure already shows the D0 first-pair outlier. The paper figure was not replaced.

Do not rerun this matrix to delete that D0 pair or to make the model regret match 1.8 s. Do not edit `Dispatcher.choose`, the guard, or the 50 tokens/ms constant. Do not start Fig. 6(a), Fig. 6(b), or Fig. 6(c). Do not start a replica-overlap sweep.

The open choice is editorial: whether the measured D1 panel, with D0 and D2 in the caption and the model number kept off the TTFT axis, belongs in the paper. A further experiment is justified only if a review requires the admission order to be the natural arrival process instead of the controlled visible-first order. That run must still use the frozen relay. It is not the next default step.

GPU time was one boot that stopped at the reset-event scorer, then one boot of 182 s that finished the pilot, 24 formal trials, and 4 D3 trials. Leave the GPUs down.
