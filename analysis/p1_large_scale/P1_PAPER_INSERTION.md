# P1 Paper Insertion

Draft only. The manuscript was not edited. Figures use Liberation Serif because Times New Roman is not installed on this machine; Liberation Serif is the metric-compatible face embedded in the PDF. Means are over three seeds. Logical workers are not GPUs.

## Experimental setup

We emulate the control plane while keeping the production gateway admission path. Each group of four logical workers replays the frozen reuse-intensive prefix pool (64 prefixes, Zipf 1.2, 35/40/25 mix of 1024/2048/4096 tokens, 25% overlap seeded onto the four workers of that group). Groups do not share prefix identities. The offered rate per group is the structure of the four-worker trace, about 1.09 compact updates/s. Residency is generated open-loop and then replayed through the same gateway for every policy. The dispatcher index uses the production last-write-wins rule. The link is a virtual 64 KB byte queue whose service rate is the previously measured FullSync drain of \(9.41\times10^{3}\) frames/s at a configured 10 Mbit/s; 2 and 5 Mbit/s scale that drain linearly and were not remeasured with `tc`. Routing false negatives count scored requests that have a resident owner of at least 512 tokens but are placed on a worker whose true coverage is below 512. Requests with no such owner are cache misses and are not counted. Coordination delay is the time from the first usable advertisement of a prefix until any owner's copy is applied, with undelivered prefixes censored at the end of a 5 s drain. This is not a GPU TTFT measurement.

## Results

At a fixed 10 Mbit/s the offered load at 4096 logical workers is 958 frames/s, so \(\rho=0.10\). FullSync, LatestOnly, AgeCov-Greedy, and KVSelect then have a routing false-negative rate of zero. Their P95 coordination delay stays at one to two frame service times (106–171 µs). RateFIFO, whose token bucket burst is one frame, is the exception: at 4096 workers its false-negative rate is 1.55% and its P95 delay is 39.8 s. Gateway CPU time for the whole 188416-event trace is about 4 s, so the idle link is not hiding a CPU bottleneck.

When the service rate is set to the offered rate divided by 1.2, before looking at which policy wins, the link is the bottleneck by construction. FullSync's false-negative rate rises from 13.5% at 4 workers to 36.8% at 4096. KVSelect stays between 1.4% and 3.3%. The reduction is mostly the replica cap of two, not the coverage limit: at 4096 workers one seed drops 57323 replica-cap updates and 5013 coverage-top-K updates. Raising \(K\) from 16 to 128 does not lower KVSelect's false-negative rate (3.27% at \(K=16\), 3.34% at \(K=128\)). BoundedSemantic, which matches the queue cap and the replica cap but does not rank by coverage, is lower still (1.57% at \(K=128\)). The trace contains no evictions, so a tombstone-priority bound matches a plain FIFO bound. KVSelect's P95 delay at \(\rho=1.2\) and 4096 workers has a three-seed range of 0.81–50.6 s; the mean of 29 s should not be read as a flat scaling law.

## Figure caption

Fig. 8. Control-plane emulation of the reuse-intensive workload. (a) P95 prefix coordination delay on a virtual link calibrated to a 10 Mbit/s FullSync drain. (b) Routing false-negative rate on the same link. Markers are means of three seeds. At 4096 logical workers the offered load is 0.10 of that drain, so every policy except RateFIFO remains at the one-frame service time with zero false negatives. RateFIFO uses a one-frame token-bucket burst. These curves are not GPU latency and are not 4096 measured GPUs. The \(\rho=1.2\) comparison is reported separately and is not drawn here.

## What not to add

Do not attribute the 10 Mbit/s result to KVSelect's top-\(K\). Do not replace the four-worker TTFT result with these false-negative rates. Do not describe 4096 logical workers as 4096 GPUs or as a multi-site deployment.
