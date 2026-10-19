# Next iteration

Do not start it from this handoff unless the user asks.

Fig. 6(a) now has a conditional result: real 1024→2048→4096 growth, zero natural merge, and a controlled inflight-window merge that cuts forwarded updates by about 55% and P95 visibility delay from 16.8 s to 4.1 s. The candidate figure already shows the natural zero. The paper figure was not replaced.

Do not run Fig. 6(b), Fig. 6(c), or Fig. 6(d). Do not rerun this matrix to chase a larger reduction. The ACK delay was frozen from the feasibility rate and must not be edited after the fact.

The open choice is editorial: whether a conditional panel belongs in the paper. A further experiment is justified only if a review requires the service limit to be a real tc qdisc instead of the relay's ACK window. That measurement has to record enqueue, send, and apply on the qdisc path. It must not reuse the F6-P3 HTB formula (`MODEL_REJECTED`). It is not the next default step, and it is not a natural-bottleneck claim.

GPU time used here was one four-worker startup plus the captures, inside a 342 s wall-clock script. Leave the GPUs down.
