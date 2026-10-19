# F6-D1 paper readiness

Case A for the load-0 condition in which the hidden owners hold the longer prefix.

The chain that was missing after F6-P2 is now measured, on this boot and this prompt:

1. The same token ids sit at 1024 tokens on two workers and 4096 on the other two. Block chains match. Response cached tokens match the warmup.
2. Frozen `choose` selects different workers under Full view and Krep=2. The Full worker is the one with 4096 cached tokens. The capped worker is the one with 1024.
3. The capped request's client TTFT is higher on all four formal seeds. Mean Δ is 1812.59 ms, median 1802.58 ms, sample SD 90.32 ms. Coverage loss is 3072 tokens on every pair.

That is a measured serving penalty for this condition. It is not the 2.0 ms queue weight from F6-P2, and it is not the 61.44 ms model regret. Those remain `MODEL_DERIVED`. The model points the same way and understates this T4 by about 30 times. A caption that prints 61 ms as the TTFT would be wrong.

## What the figure is allowed to say

Fig. 6(d) can show measured client TTFT for Full versus Krep=2 when a controlled admission order hides the 4096-token owners and load is 0. The candidate is `fig6d_candidate.pdf`. It was not swapped into the paper.

The caption has to carry the controls:

- D2, where the long prefix is already visible: route unchanged, mean Δ 0.91 ms.
- D0, equal coverage: coverage loss 0, model regret 0. Median Δ is -1.10 ms. The mean is -317 ms because the first formal pair, same worker and same 4096-token hit, took 1390 ms and then 124 ms. That pair is drawn. It is not a gain from suppression.
- Assignment A and B both produce the D1 gap, so it is not one GPU.
- Admission order is controlled. It is not the natural arrival process.
- The publisher is `EXPERIMENTAL_WRAPPER`, not `publish_state`.
- No server TTFT, no claim that this lowers TTFT in the paper's main workload, and no production-congestion wording.

D3, two pairs with a real holder on the visible workers, has larger deltas (4072 ms and 5485 ms) and a wider spread. It can be a sentence. It should not be the axis that sets the claim, and it should not be dropped.

## What is still not shown

Natural advertisement order. A partial eviction. A calibrated replacement for the 50 tokens/ms constant. Any scenario except this 4128-token completion on four T4s. TTFT of a request the router did not choose.
