# Next iteration

Do not start P0-D2. Do not add an Adaptive expansion rule, and do not change Algorithm 1 or the default K.

The ov30 sweep already answers the expansion question at K=32 and K=64. K=24 would sit between two points that do not improve TTFT. Running it would not turn a flat TTFT curve into a reason for a controller.

The online signals now on the relay, pending depth and Top-K drop count, are high on the base workload where the false-negative rate is 0. A prototype that expanded on those signals would expand in the cells that do not need it.

A later round is justified only if the question changes from "should K grow under pressure" to "is the K=8 FN advantage on ov30 real". That would be a preregistered static comparison, not an adaptive prototype, and it is not this round.
