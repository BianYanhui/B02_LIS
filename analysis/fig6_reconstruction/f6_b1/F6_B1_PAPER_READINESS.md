# F6-B1 paper readiness

Conclusion for this round: `CONDITIONAL_PRIORITY_BENEFIT`, with a decision-level routing effect under the controlled arm and an executed check of the two dispatcher states.

The chain that was measured:

1. Worker 0 really lost prefix X by `reset_prefix_cache`. Native event: `AllBlocksCleared` before the trigger store. Worker 1 was not cleared.
2. The experimental adapter emitted one matching `K_TOMB` per seed. The dispatcher removed only that owner.
3. With a frozen one-frame ACK window, seven background upserts (four in the pilot) were still queued when the tombstone arrived. Priority forwarded it next. FIFO forwarded it after those upserts.
4. At the probe frozen before the formal seeds, FIFO's `choose` stayed on worker 0 and priority's `choose` moved to worker 1. The counterfactual without the stale entry also selects worker 1.
5. Two rebuilt stale worlds sent a real request to worker 0 and observed `cached_tokens = 0`. Two rebuilt cleared worlds sent one to worker 1 and observed `cached_tokens = 4096`.

Natural delivery had an empty queue. FIFO and priority agreed. That arm is a negative control.

## What can go in the paper

A conditional sentence is supported: when unsent upserts are held by a one-frame ACK window, invalidation priority shortens tombstone queue time by about 5.56 s on these three seeds and removes the stale-positive routing decision at a preregistered probe. The rate to plot is the decision-level invalid-route rate, 3/3 versus 0/3 under that window, and 0/3 versus 0/3 with natural delivery. The duration belongs in the text, labeled as capture detection plus timed-replay apply delay.

The candidate figure is `fig6b_candidate.pdf`. It does not replace the paper's Fig. 6.

## What cannot

- A benefit on the natural four-GPU control path. The natural queue was empty.
- A 10 Mbit/s or `tc` bottleneck. The hold is `max_inflight = 1` plus an ACK sleep of 0.792 s, chosen from this pilot and frozen.
- An executed FIFO-versus-priority request rate. The executions rebuild the stale view and the cleared view. They do not ride the two relay policies.
- A TTFT improvement. One cleared hit took 125 ms and the other 1548 ms.
- A production `publish_state` that maps `BlockRemoved` by itself. The adapter is experimental, and the loss is a controlled reset.
- Statistical significance from three seeds.
- Any reading of an empty tombstone sample as a delay of 0. This round's primary cells all have a measured apply time.

F6-P0 remains an offline mechanism check. It is not this result.
