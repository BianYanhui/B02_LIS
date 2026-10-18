# F6-P3 Fig. 6(d) — Stable held load and TTFT pairing

One four-T4 boot. Model, flags, and prefix recipe match F6-P2. Qwen2.5-1.5B-Instruct, prefix caching, block size 16, cache salt `f6p3-prefix-seed1`, digest `F6P3D`, seed 1. Admission order for the cap replay is 0, 1, 2, 3. `rr = 0`. Merge, priority, adaptive, and top-k are off. The cap is the frozen replica rule.

Holder placement was fixed before the run:

- D-positive: one 640-token request on workers 0 and 1. Target running vector `[1,1,0,0]`, DeltaLoad `+1`.
- D-negative: one 640-token request on workers 2 and 3. Target `[0,0,1,1]`, DeltaLoad `-1`.

DeltaLoad is `min(load 0, load 1) - min(load 2, load 3)`. Loads are harness in-flight counts. A sample is `ENGINE_CONFIRMED` only when all four `vllm:num_requests_running` gauges equal that vector and all four waiting gauges are 0. No load number was written into the dispatcher.

If the target was not confirmed within 5 s, the phase would record four fallback samples and stop. No requests were added after the fact. Both phases confirmed on the first wait. Four samples were then taken 0.50 s apart. All eight samples are engine-confirmed and match the target. Active measurement was 38.3 s.

## Residency

OBSERVED. Warmup cached tokens were 0 on all four workers. Probe and post-phase reprobe cached tokens were 1056 on all four. Prefix-cache hit deltas on the probe were +1056. Prompt length 1083. Common prefix 1068 tokens, block-aligned 1056. Client prefix sha256 `4474f9646584c9b66181bc7c234cda95c0f18abb75f49fa11f33839f3f4760f3`. Engine block hash `NOT_MEASURED`.

`kv_cache_usage_perc` during the holds was about 0.0006 on the busy workers and 0 on the idle workers. That gauge is not the residency proof.

The engine was idle before each phase and after each phase. The negative phase therefore started from the same empty running set, with the prefix still resident. The two load vectors were recovered as separate holds, not as one arrival ramp.

## Model regret

MODEL-DERIVED. `T_i = load_i * 2 ms + max(0, 1083 - 1056) / 50`. The prefill term is 0.54 ms on every worker. It cancels. Regret is `T(cap) - T(full)`.

| Phase | Confirmed samples | Vector | DeltaLoad | Full selected | Krep=2 selected | Regret | Visible / hidden |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| D-positive | 4/4 | [1,1,0,0] | +1 | 2 | 1 | 2.0 ms | 0,1 / 2,3 |
| D-negative | 4/4 | [0,0,1,1] | -1 | 0 | 0 | 0.0 ms | 0,1 / 2,3 |

Costs at DeltaLoad `+1` are `[2.54, 2.54, 0.54, 0.54]`. The hidden workers are the lighter model choices. The cap cannot see them and selects worker 1. Full view selects worker 2. Both selections sit in the model argmin of their own visible set. Full view's choice is in the global argmin.

Costs at DeltaLoad `-1` are `[0.54, 0.54, 2.54, 2.54]`. Both views select worker 0. Regret is 0.

The four samples inside a hold are one stable state, not four seeds. No significance test. The 2.0 ms figure is one queue penalty. It is not a TTFT difference.

Holder requests used a different salt and a 49-token prompt. Their own TTFTs were about 46–59 ms and their decode latencies were about 15–16 s. Those numbers are the holders that created the load. They are not a full-view versus cap-view comparison.

## TTFT pairing

`TTFT_PAIRING_NOT_READY`. No paired serving request was sent.

The positive phase does select different workers (2 versus 1), so a comparison would have been about two real engines. The following conditions are not available:

- There is no prefix-cache snapshot that can be restored without `reset_prefix_cache`.
- A request to the full-view worker changes that worker's KV suffix and its running gauge before the cap-view request can start.
- A residency check between the two requests is itself a request.
- The held loads differ across the two selected workers by construction. Measuring both at once would add two extra requests.
- `reset_prefix_cache` would drop the shared prefix and would not preserve the in-flight holders.

The probe TTFTs (about 1.4 s) are residency checks. They are not routing-regret measurements.

## Verdict inputs

Stable held load is repeatable on this boot for both signs. The conditional model result from F6-P1/P2 still holds when the vector is held instead of ramped: DeltaLoad `+1` gives 2.0 ms, DeltaLoad `-1` gives 0. Equal coverage is why the prefill term cancels. A formal TTFT matrix is not justified.
