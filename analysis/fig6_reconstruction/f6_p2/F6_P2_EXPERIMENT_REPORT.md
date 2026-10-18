# F6-P2 experiment report

Date: 2026-10-19. Branch `f6-p2-replica-tradeoff`, parent `78b6c1c`. Three short cells on one vLLM boot. No change to `experiments/`, the relay, `Dispatcher.choose`, formal4t4, Run A, Run B, F6-P0, or F6-P1.

## Verdict

**Case A: conditional trade-off confirmed**, for model cost on one shared prefix.

Forward arrival reproduced the 2.0 ms model regret, including under seed 2. Reverse arrival, where the visible owners are the lighter ones, had model regret 0 on every engine-confirmed sample. Interleaved arrival had 2.0 ms only on the sample whose hidden owners were strictly lighter. The 2.0 ms is **MODEL-DERIVED**. It is not a measured TTFT.

## What ran

Four host vLLM 0.10.2 processes, one per Tesla T4, ports 8000–8003, same flags as `experiments/4t4/restart_4t4.sh`. Logs are under `analysis/fig6_reconstruction/f6_p2/server_logs/`. `restart_4t4.sh` was not used, because it truncates `analysis/formal4t4/server_logs/`. `b02-gateway4t4` on `127.0.0.1:9710` was not touched.

The three cells shared that boot. Each cell had its own cache salt and digest, so the prefix namespace started cold: warmup cached tokens were 0 on every worker. The engine was idle before and after every load window. Active measurement for all three cells was 49.6 s.

Holder timing was fixed before the run and was not edited afterward:

- decode length 320 tokens, so a holder outlives the 3.5 s launch window
- launch interval 0.50 s
- first sample 0.25 s after the first launch, then every 0.50 s, eight samples
- round-robin cursor fixed at 0
- admission order fixed at 0, 1, 2, 3

`ENGINE_CONFIRMED` means harness in-flight equals `vllm:num_requests_running` on all four workers and `num_requests_waiting` is 0. Every predetermined sample is in the CSV. Harness-only rows stay in the file and stay out of the means below.

## Residency

| Cell | Salt | Warmup cached | Probe cached | Hit-counter delta | Reprobe cached |
| --- | --- | --- | ---: | ---: | ---: |
| A-reverse, seed 1 | `f6p2-A-reverse-seed1` | 0,0,0,0 | 1056 ×4 | 1056 ×4 | 1056 ×4 |
| B-interleaved, seed 1 | `f6p2-B-interleaved-seed1` | 0,0,0,0 | 1056 ×4 | 1056 ×4 | 1056 ×4 |
| C-forward, seed 2 | `f6p2-C-forward-seed2-seed2` | 0,0,0,0 | 1056 ×4 | 1056 ×4 | 1056 ×4 |

Prompt length was 1083. The client-side common prefix is 1068 tokens, block-aligned to 1056. Engine block hashes were not read. ShadowCache was not used.

## Load samples

Primary load is harness in-flight, the integer `Dispatcher.loads` uses. The gauge is the confirmation check.

| Cell | Order | Engine-confirmed ramp samples | Harness-only | Confirmed loads with regret 2.0 ms |
| --- | --- | ---: | ---: | --- |
| A | 3,2,1,0,3,2,1,0 | 4 of 8 | 4 | none |
| B | 0,2,1,3,2,0,3,1 | 8 of 8 | 0 | `[1,1,1,0]` |
| C | 0,1,2,3,0,1,2,3 | 8 of 8 | 0 | `[1,1,0,0]`, `[1,1,1,0]`, `[2,2,1,1]`, `[2,2,2,1]` |

Cell A’s second wave is harness-only: the harness counted two requests while the engine still showed one on three of the workers. Those four rows have model regret 0 and are not in the confirmed mean.

Cell C confirms the two vectors F6-P1 already confirmed, and also confirms the second-wave vectors F6-P1 had to leave as harness-only. Seed 2, same forward order.

On every sample, including harness-only, `Krep=2` forwarded 2 advertisements and dropped 2. Visible owners were 0 and 1. Suppressed owners were 2 and 3.
