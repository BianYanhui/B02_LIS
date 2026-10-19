# Data and implementation audit

No gateway admission rule was changed. `git diff 5d2d300 HEAD -- experiments` is empty, so the Top-K code that produced `formal_A_5d2d300` is the code on this branch.

## What K is

`global_topk` is the maximum number of distinct pending digests in `topk_best`. It is not a forwarding-rate limit.

The reject happens in `Relay.enqueue`, before the frame joins the FIFO. A digest already in the set is kept. A new digest is dropped when the set is full and its coverage does not beat the worst member. After the last queued copy of a digest is forwarded or superseded, `_topk_release` frees the slot, so a later update of that digest can enter again. A drop is not a permanent loss. Frames already written to the socket are not pulled back.

`StaticTopK8` and `StaticTopK16` use the same tuple except the last field: `(4, 1, 1, 0, 2, K)`. Mode 4 is static, merge is on, priority is on, adaptive is off, replica cap is 2. The smoke cell confirmed the cap: `relay_max_queue=8` and `relay_drop_global_topk=649201` under StaticTopK8.

## Where `forwarded_per_s` is counted

`Relay.forwarded` increments after `down_writer.write` and `await down_writer.drain()`. That is a gateway socket write of a frame that has already passed admission and been dequeued. It is not the admission count, and it is not incremented at dispatcher apply.

`experiments/icc_kv/e2e.py` stores `forwarded_per_s = forwarded_end / noise_window`. `forwarded_end` is that counter at the moment background noise stops. `noise_window` is the noise interval. The same summary also stores `dispatcher_frames`, which is the dispatcher `on_frame` count, and `foreground_applied`, which is the routable subset.

The 9408 frame/s figure is `c_drain_per_s` in `analysis/icc_kv/pc10m_a8a07b7_r1/capacity_window.json`: 9407.55. It is the median of six FullSync rows in `path_capacity.json` at commit `a8a07b7`. Each row's `forwarded_per_s` is the same `relay_forwarded` counter divided by the offer interval. The wire size in that file is 104 bytes, and `tc_link_bit_s` is 10000000. Those six rows span 9063 to 10195 frame/s. The spread is 0.120, which is why the file sets `c_drain_spread_warning`. Three higher-offered FullSync rows (forwarded 6621, 6362, 6283) are excluded because offered/forwarded exceeded 1.5; the capacity comment says ACKs share the HTB parent, so the forward drain falls as ingress rises.

`git diff a8a07b7 5d2d300 -- experiments/icc_kv/wire.py experiments/4t4/net/gateway_relay_4t4.py` is empty. The counter and the frame size did not change between the capacity scan and the K sweep.

K=16 on the ov30 ultrahigh cells forwards about 10148/s. That number is the same counter and the same 10 Mbit HTB ceiling. It sits inside the FullSync plateau's own range (9063–10195), near the top of it. It is not a different layer, and it is not evidence that the link carried more than the measured drain spread. The smoke cell's dispatcher frame count equalled `relay_forwarded` (473079), so at the K=8 rate the dispatcher was applying what the gateway wrote. The formal summaries do not publish a separate dispatcher-apply rate for the 600 s cells beyond `dispatcher_frames`.

## What is paired inside the ov30 sweep

`ksweep_ov30` ran in one process at commit `5d2d300`, 10 Mbit, capacity denominator 9407.55, arrival 0.237, burst multiplier 1.5, invalidate every 8, 4 noise workers, 8 noise senders, concurrency 4, shadow capacity 104544, cell 600 s, seeds 0–2. The runner log is `analysis/icc_kv/formal_A_5d2d300/runner.log`. Useful pool 32, useful mix `1024:1,2048:1,4096:1`, noise mix `256:70,1024:10,2048:10,4096:10`.

For a fixed seed, every method draws requests from `random.Random(seed)`. The 133 scored requests match on slot and coverage across K. Background noise restarts at `seq0=100_000_000` every cell. Each cell calls `reset_prefix_cache` and uses a new cache salt, so the physical token bytes are not shared across cells. Routing policy, merge, and the replica cap are the policy tuple above. Method order inside a seed is shuffled, so the cells are not simultaneous.

K=24 remains `NOT_MEASURED`.

## What is not a strict pair with today's base run

The October base K=16 cells used the same runner flags and the same experiment tree. They are not a same-session pair with a K=8 run started on 2026-10-19. The October vLLM process flags are not stored in `e2e_summary.json`. This round therefore reran StaticTopK8 and StaticTopK16 in one process, seeds 0–2, scenario ultrahigh, base workload (pool 16, noise coverage 256, overlap 0), and the same CLI values as the `main-base` line in that runner log. The vLLM processes started for this round report `KV_CACHE_TOKENS=104544`, which is the capacity the formal runner passes in. The historical base rows stay a reference. The TTFT comparison used for the crossover is the new pair.

The rerun is commit `198a5b62155cad749df5ff43a91c55c66e498692`, `git_dirty_files` empty, `tc_link_bit_s` 10000000, wire 104 bytes. Smoke was one 60 s StaticTopK8 cell: `relay_max_queue=8`, `relay_drop_global_topk=649201`, `relay_forwarded=dispatcher_frames=473079`, failed requests 0. The formal directory `base_ultrahigh` has six cells, exit 0, no `cell_errors.log`. On every formal cell `dispatcher_frames` equals `relay_forwarded`.

October base K=16 mean TTFT was 3321–3767 ms with reused tokens 2952–3139. The same-session rerun is about 588–678 ms with reused tokens 3293–3386. The reuse level is similar and the TTFT level is not. Absolute TTFT is not compared across those two days. Both new K values are the pair.

The base run is a controlled 10 Mbit overload at ρ=2. It is not a trace of natural deployment congestion. `prepare_fixed_gateway` recreated container `b02-gateway4t4` with the same 1 CPU and 10 Mbit HTB the formal matrix uses. That is the paired control path, not a change to admission.
