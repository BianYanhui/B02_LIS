# F6-A2 paper readiness

## Case

**Case C.**

Real coverage growth is observed. Natural arrivals produce no pending overlap and no signaling or delay difference. Under one frozen inflight window, merge-on removes only true coverage-increasing updates, forwards fewer frames, and shortens latest-coverage visibility delay. That is a conditional mechanism chain. It is not a natural-congestion result.

It is not Case A, because the natural arm is a zero. It is not Case B, because the controlled arm does move the delay. It is not Case D: the gateway, not only the wrapper, merged increasing coverages. It is not Case E: residency and the replay timestamps were measured.

## What can be written

A narrow Fig. 6(a) caption can say:

- The same vLLM growth trace, three seeds, two arms.
- Natural: reduction 0.
- Controlled inflight window, not a 10 Mbit claim: mean forwarded-update reduction 0.549 (sample SD 0.012), every removed frame a true extension.
- P95 latest-coverage visibility delay on that window falls from 16.84 s to 4.09 s.
- Final coverage still matches. No TTFT.

The candidate figure is `fig6a_candidate.pdf`. It keeps the natural zero. It is not the paper figure.

## What cannot be written

- Extension aggregation on the natural four-GPU path. F6-F1's same-coverage 20.8% result is a different workload and stays what it was.
- The ACK window as the production bottleneck. P0-C1 still says the measured block-event load did not fill the indexer.
- Wrapper message reduction. ZMQ messages and wrapper upserts are both 168. The 85 hashes per `BlockStored` are vLLM's own batch.
- Faster routing, lower TTFT, or a deployed wrapper. The sender is `EXPERIMENTAL_WRAPPER`.
- A lossless block lifecycle. Nothing was evicted.

## Readiness

Usable as a conditional extension-supersession panel if the caption states the natural zero and the controlled window. Not usable as an unqualified claim that extension aggregation relieves natural control-plane congestion.
