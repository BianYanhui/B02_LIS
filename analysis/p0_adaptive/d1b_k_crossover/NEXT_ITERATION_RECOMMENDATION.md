# Next iteration

Do not start P0-D2. Do not add an Adaptive rule, and do not change Algorithm 1 or the default K in the code.

The same-session base pair removed the missing comparison. K=16 does not buy TTFT or reuse there. K=8 does not lose them. The ov30 FN advantage of K=8 is consistent across the three seeds and its TTFT advantage is not. That is not a crossover, and the control signals cannot see a regime boundary that is not in the user metrics.

A later round may preregister a default-K check if the paper wants the published static point to be 8 rather than 16. That is a static choice, not a controller, and it is not this round. Do not add K=24 to fill the table. Do not retune ρ to look for a crossover that these two workloads did not show.
