# Motivation possibilities on 4× T4

2026-10-17. Exploration only. vLLM was started with the existing `restart_4t4.sh` (gpu memory 0.40, max model len 6144, block size 16, prefix caching on) and stopped afterwards. GPUs are idle again. No experiment code and no paper text were changed.

The question was whether some other reading of this machine could still support a control-plane congestion motivation. Four readings were checked. One of them is real and still too small for a 10 Mbit drain. One of them is real and supports a different claim.

## What the machine actually does

Each T4, at the formal startup settings, has a GPU KV cache of 104,544 tokens, 6,534 blocks of 16. That matches the shadow capacity used in formal4t4. `vllm:kv_cache_usage_perc` stayed at 0.0 through the whole probe, including after prefixes were demonstrably cached, so that gauge is not evidence of an empty cache.

`vllm:prefix_cache_queries_total` counts queried tokens, not messages. A 1,028-token prompt incremented it by 1,028 on both the cold call and the repeat. Hits incremented only on the repeat, by 1,024, which is the block-aligned prefix. The API reported `cached_tokens = 1024` and the repeat took 0.058 s against 0.360 s cold. A longer first call, 2,055 tokens, took 8.61 s and the repeat took 0.224 s with 2,048 cached tokens; that first call also paid engine warmup, so the 0.360 / 0.058 pair is the cleaner hit/miss gap.

## Four directions

### 1. Advertise one message per KV block

This is the only direction that raises the offered rate by a large factor without adding synthetic noise.

A cold unique prompt allocates about one block per 16 tokens. On one warm T4, 90 unique 1,030-token prompts produced 3,527 prompt tokens/s, which is 220 new blocks/s. Across four T4s, 32 unique prompts finished in 5.87 s and the four engines together counted 5,647 query tokens/s, which is 353 block lookups/s, all misses. If the four cards each sustained the single-card rate, the upper figure is about 880 blocks/s. The burst did not actually reach that.

At the paper's 104-byte accounting convention:

| Stream | frames/s | bit/s at 104 B |
|---|---:|---:|
| Formal wrapper, Original Ideal | 2.07 | 1.7×10³ |
| Measured 4-GPU unique block lookups | 353 | 2.9×10⁵ |
| 4 × one-GPU unique rate, not measured as a simultaneous run | 880 | 7.3×10⁵ |
| Configured token bucket | 200 | 1.7×10⁵ |
| 10 Mbit tc drain | 9,408 | 1.0×10⁷ |

353 frames/s is above the configured 200, and 220 frames/s on one GPU already meets it. Both are far below 9,408. The measured cluster rate would have to be about 27 times higher to fill that drain. The 880 frame/s extrapolation would still need about 11 times more. Sending one frame per token instead of per block would use the 5,647 token/s measurement directly and land near 4.7 Mbit/s. That is a protocol this stack does not implement. vLLM's own cache lookup is in-process; nothing in the formal path emits those lookups onto the control network.

Possibility: real, if the paper switches the advertisement unit from one completion to one new block, and the workload is mostly compulsory misses. It can cross an arbitrary 200 frame/s budget. It does not fill a 10 Mbit drain on these four cards.

### 2. Physical eviction, once the cache is actually full

The formal baseline emits zero tombstones because a pool of a few dozen prefixes does not fill 104,544 tokens. That part of P0-A1 stands.

On GPU 0 the probe then kept sending unique prompts past that capacity. Afterwards:

| Prefix | When it was first inserted | Replay |
|---|---|---|
| flood-0 | early in the unique stream | miss, 0.288 s, `cached_tokens` absent |
| plant-0 | before that stream | hit, 0.054 s, 1,024 cached tokens |
| extra-0 | near the end | hit, 0.050 s, 1,024 cached tokens |

So a prefix that had been computed was later gone, while a later one was still present. This is a real eviction, not a shadow LRU event. The survival order was not isolated, so this run does not identify the replacement policy. It also does not give an eviction message rate. If a full cache under unique traffic emitted one tombstone per new block, the block rate above would roughly double, to somewhere around 400–1,800 frames/s on four cards. That is still under the 10 Mbit drain.

Possibility: real churn exists once the working set exceeds one GPU cache. The formal workload never enters that regime. Doubling the block rate still does not motivate a 10 Mbit bottleneck.

### 3. Bursts, rather than the mean

Ideal Original rep 0, using completion time as dispatch time plus TTFT: mean about 3.5 completions/s inside the measured window, at most 9 in any 1 s, at most 3 in any 100 ms. Nine completions times 64 blocks of a 1,024-token prefix is under 600 block events in that second. The peak does not reach 9,408 frame/s.

Possibility: closed for the 10 Mbit claim. A 200 frame/s budget can be crossed by a short burst even at the wrapper granularity only if many completions land together; 9 wrapper upserts in one second does not.

### 4. The control decision matters even when the pipe is not full

This one is supported, and it is not a congestion result.

A wrong worker pays the cold prefill. On a warm engine that was 0.360 s versus 0.058 s for a 1,028-token prompt, about six times. The formal Ideal cells already apply state locally and show zero view-missing. FullSync at the paper's ρ=0.5 still shows about 5% missing on Original and 16% on Reuse, with higher TTFT than Ideal. At that ρ the HTB rate is a few kbit/s, so one 104-byte frame takes on the order of 0.2 s. The misses there come from a deliberately slow pipe, not from a 10 Mbit or 100 Mbit link. On 10 Mbit the same frame is about 80 µs, which is far below the repeat interval of this workload.

Possibility: strong as a reason to propagate KV state at all. It does not show that four T4s congest a realistic control network. It fits the P0-A2 wording, budget-constrained coordination: the budget is chosen, and inside that budget a stale or dropped advertisement costs a cold prefill.

## What this does not reopen

Linear worker scaling of the wrapper rate, the full-table refresh assumption, and the 200 frame/s default were settled in P0-A2. This pass did not add synthetic noise and did not change those conclusions. The new measured quantity is the block lookup rate under unique prefill, and it sits between the two capacities already on the table: above 200 frame/s, an order of magnitude under the 10 Mbit drain.

## If one of these is pursued

The block-advertisement reading is the only congestion-shaped follow-up that this machine can feed. It needs an explicit statement that the control message is one per new block, a workload that is mostly compulsory misses, and a capacity that is not defined as offered/ρ. Even then the expected load on four T4s is a few hundred to about a thousand frames/s. A hit/miss TTFT figure can support placement value. Neither result turns the natural wrapper rate into a 10 Mbit problem.
