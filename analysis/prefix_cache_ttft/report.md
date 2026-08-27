# Prefix-cache TTFT diagnostic

## Setup

- `/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct`, one vLLM at `http://127.0.0.1:8010`, concurrency = 1.
- No dispatcher, gateway, tc, signaling, or multi-instance scheduling.
- Hit reuses a completed prefix with a changed suffix; Miss uses a new leading prefix and cache salt.

## Results

| Prefix | Miss TTFT | Hit TTFT | Saving | Saving | Miss cached | Hit cached |
|---:|---:|---:|---:|---:|---:|---:|
| 512 | 149.2 ms | 44.4 ms | 104.9 ms | 70.3% | 0.0 | 528.0 |
| 1024 | 370.6 ms | 54.5 ms | 316.1 ms | 85.3% | 0.0 | 1040.0 |
| 2048 | 845.6 ms | 66.0 ms | 779.6 ms | 92.2% | 0.0 | 2064.0 |
| 4096 | 2267.4 ms | 98.0 ms | 2169.4 ms | 95.7% | 0.0 | 4112.0 |

## Interpretation

1. 512-token Hit/Miss gap: 104.9 ms.
1024-token gap: 316.1 ms.
2048-token gap: 779.6 ms.
4096-token gap: 2169.4 ms.
2. Saving increases between the shortest and longest tested prefix.
3. Hit/Miss validity is confirmed by low miss cached tokens and materially higher hit cached tokens; the pre-run sanity gate aborts otherwise.
4. This isolates Prefill/KV reuse.  It cannot attribute any main-experiment effect to dispatch or signaling.
5. The isolated trend is consistent with short reusable prefixes contributing to a weak end-to-end TTFT effect on Qwen2.5-1.5B/T4.
