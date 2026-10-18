# F6-P1 observed snapshot

Date: 2026-10-18. Branch `f6-p1-observed-replica`, parent `36f3bfa`. One GPU cell, seed 1. No change to `experiments/`, the relay, `Dispatcher.choose`, formal4t4 results, Run A, Run B, or F6-P0.

## Verdict

**P1-PASS**, scoped to one shared prefix and one pre-registered round-robin arrival.

Four real vLLM workers held the same prefix. `Krep=2` kept owners 0 and 1 and dropped 2 and 3. On two engine-confirmed load snapshots, the cap view’s model cost was 2.0 ms higher than the full view. That 2.0 ms is `MODEL-DERIVED`. It is not a measured TTFT difference.

## What ran

Four host vLLM 0.10.2 processes, one per Tesla T4, ports 8000–8003. The command matches `experiments/4t4/restart_4t4.sh`: Qwen2.5-1.5B-Instruct, `--gpu-memory-utilization 0.40`, `--max-model-len 6144`, `--max-num-seqs 8`, `--enable-prefix-caching`, `--enable-prompt-tokens-details`, `--block-size 16`, `--enforce-eager`. `VLLM_SERVER_DEV_MODE=1`. Logs are under `analysis/fig6_reconstruction/f6_p1/server_logs/`. The script was not used, because it truncates `analysis/formal4t4/server_logs/`.

`b02-gateway4t4` on `127.0.0.1:9710` was not stopped or reconfigured. No gateway, tc, or Dynamo path was used. Replica admission was the in-process frozen `Relay.enqueue` / `_admit_replica`.

GPU KV cache size on worker 0: 104,544 tokens. Active measurement after the servers were ready: 14.7 s. The warmup wave took 6.7 s, under the 120 s stop.

## Prefix

One lineage, digest `F6P1X`, cache salt `f6p1-seed1`, 1024 repetitions of `context `.

| Step | What was sent | Cached tokens | Prompt tokens | TTFT |
| --- | --- | ---: | ---: | ---: |
| Warmup, turn 0, all four workers | first prefill | 0, 0, 0, 0 | 1083 | 5.38–5.43 s |
| Probe, turn 1 | same prefix, new suffix | 1056, 1056, 1056, 1056 | 1083 | 1.57–1.60 s |
| Reprobe, turn 2, after the load window | same prefix, newer suffix | 1056, 1056, 1056, 1056 | 1083 | 90–101 ms |

Client tokenizer: the two chat-templated prompts share 1068 tokens. The largest multiple of the block size is 1056. Each probe’s `prefix_cache_hits` rose by 1056 and `prefix_cache_queries` rose by 1083. That is the engine hit counter, not ShadowCache.

Engine block hashes were not exported. The client SHA-256 of the shared token-id prefix is `a0c92e73685889a436edaafe47579af53b47baed287a82c208ee5f3f8a84ac7d`. Lineage identity is the digest string plus this salt. Coverage used later is the measured 1056, not the 1024 word count.

`kv_cache_usage_perc` stayed near 0.001 during the load window. In this vLLM, the free-block queue includes unreferenced prefix blocks, so that gauge does not say the prefix was absent. The probe and the post-window reprobe do.

## Load

Primary load is the harness in-flight count: incremented when a request is launched, decremented when its stream finishes. That is the integer `Dispatcher.loads` uses. `vllm:num_requests_running` and `num_requests_waiting` are recorded beside it. Waiting was 0 on every sample.

Scenario A, after the probes had finished: harness `[0,0,0,0]`. Four decisions differ only by the round-robin cursor `rr = 0,1,2,3`.

Scenario B was fixed before the run and was not edited afterward: eight short holders, `max_tokens = 96`, cache salt `f6p1-load-seed1`, workers `[0,1,2,3,0,1,2,3]`, 300 ms apart. All eight holders returned ok. Eight samples were kept, including the ones with regret 0. The arrival order starts at worker 0, which is also the first owner the cap keeps. The early samples therefore leave workers 2 and 3 idle because their jobs have not started. That is a real in-flight state, and it is also the limit on how far this cell generalizes.

| Snapshot | Harness in-flight | vLLM running | Gauges agree |
| --- | --- | --- | --- |
| B0 | 1,0,0,0 | 0,0,0,0 | no; harness is ahead of the engine |
| B1 | 1,1,0,0 | 1,1,0,0 | yes |
| B2 | 1,1,1,0 | 1,1,1,0 | yes |
| B3 | 1,1,1,1 | 1,1,1,1 | yes |
| B4 | 2,1,1,1 | 1,1,1,1 | no |
| B5 | 2,2,1,1 | 1,1,1,1 | no |
| B6 | 2,2,2,1 | 1,1,1,1 | no |
| B7 | 2,2,2,2 | 1,1,1,1 | no |

Rows for both scenarios are in `observed_worker_snapshots.csv`.
