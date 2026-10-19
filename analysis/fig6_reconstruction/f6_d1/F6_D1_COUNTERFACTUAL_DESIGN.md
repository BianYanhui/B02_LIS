# F6-D1 counterfactual design

This file is the protocol fixed in `run_f6_d1.py` before the GPU run. It is not a result.

## Two worlds, one prompt

Each Full / Krep=2 pair uses the same token-id list. The lineage nonce is `F6D1S{seed}{scenario}`. It does not contain the view. The filler token is the single id 264 (`" a"`). The measured prompt is the 4096-token prefix plus 32 suffix tokens of id 293 (`" b"`), length 4128, block size 16.

Workers do not get different prefix text. They get different warmup lengths of that shared prefix. Warmup is `POST /v1/completions` with the token-id list, `temperature=0`, `max_tokens=1`. The measured request is a separate streamed call, `max_tokens=4`, sent only to the worker `Dispatcher.choose` returned.

A chat template is not applied. Token prefix is the list prefix, checked before the request is sent.

## Cache isolation

Salt alone cannot separate the two views, because the views must share token ids. Between worlds, and only after `num_requests_running` and `num_requests_waiting` are 0 on all four workers, the driver calls `POST /reset_prefix_cache` on the four vLLM processes started for this run. Those processes were started with `VLLM_SERVER_DEV_MODE=1`. The production gateway is not reset.

The next warmup is the residency check. A cold prefix omits `prompt_tokens_details` when cached tokens are 0; the driver records that omission as 0. If a warmup reports cached tokens other than 0, or the block chain does not match, or coverage is not the target, the trial is invalid and is kept. The target length is not substituted for the measured coverage.

vLLM publishes `AllBlocksCleared` on the next scheduler step, so the reset event arrives in the warmup's own batch, before that step's `BlockStored` events. A clear ordered before the first store is the deferred reset. A clear ordered after a store is an eviction and fails the trial. The first pilot, saved under `raw/pilot_reset_event_window/`, counted every clear as an eviction and stopped. The chain already matched and cached tokens were 0. That scorer cut was corrected before any TTFT was measured. Seeds, lengths, and the routing rule were not changed.

`reset_prefix_cache` is not called while a holder is in flight.

Different seeds and scenarios differ inside the first 64 token ids, so a missed reset cannot make one lineage hit another. The reset is still required so that Full and Cap of the same lineage both start cold.

## Order and GPU identity

Formal seeds, fixed before the run:

| Seed | Assignment | World order |
| --- | --- | --- |
| 2026101910 | A, visible {0,1}, hidden {2,3} | Full then Cap |
| 2026101911 | B, visible {2,3}, hidden {0,1} | Cap then Full |
| 2026101912 | A | Cap then Full |
| 2026101913 | B | Full then Cap |

Pilot seed `2026101909`, assignment A, scenario D1 only, Full then Cap. It is not in the formal means.

Scenarios:

- D0: 4096 / 4096 / 4096 / 4096, load 0. Equal-coverage control.
- D1: visible 1024, hidden 4096, load 0. Hidden owners hold the longer cache.
- D2: visible 4096, hidden 1024, load 0. The longer cache is already visible.

Advertisements go through the frozen relay, visible owners first. Label: `CONTROLLED_ADMISSION_ORDER`.

Each world constructs a new `Dispatcher` with `rr=0` and loads taken from the confirmed running gauges. For D0–D2 the planned vector is `[0,0,0,0]`. A world whose gauges are not that vector does not enter the primary pair.

## TTFT

Client TTFT starts when the streamed POST is issued and stops at the first SSE choice whose `text` is non-empty. HTTP headers, empty events, and the usage chunk are not a token. If no token arrives, the delay is empty. It is not stored as 0.

Server-side TTFT is `NOT_MEASURED`. It is not mixed into the client series.

The warmup request's latency is not a routing TTFT. A D3 holder request's latency is not a routing TTFT.

## Validity of a pair

A pair is primary only when both worlds have matching prompt hashes, block-chain residency at the target coverage, cold warmup cached tokens, the planned owner set, the planned gauges immediately before the request, a client TTFT, prompt length 4128, and response `cached_tokens` equal to the selected worker's warmup coverage.

Invalid trials stay in `f6_d1_per_trial.csv`.

The comparison is a repeated-trial counterfactual. It is not two potential outcomes observed at the same instant. Seed is the repetition. Points inside one seed are not extra seeds. With four pairs there is no significance test and no P95 claim.

Difference-in-differences is `mean DeltaTTFT(D1) - mean DeltaTTFT(D0)` and the same for D2, on valid pairs, plus the assignment A/B split. It does not remove every confound.

## D3, preregistered and conditional

D3 is visible 1024, hidden 4096, with one real in-flight completion (`max_tokens=256`, a 64-token prompt that is not a prefix of the measured prompt) on each visible worker. The target running vector is `[1,1,0,0]` for assignment A and `[0,0,1,1]` for assignment B. Confirmation is the four `vllm:num_requests_running` gauges and waiting 0, within 5 seconds. No extra holder is added if that fails.

D3 runs only when all 12 formal pairs are valid, every formal trial was idle before its reset, and wall time from process start is still under 25 minutes. Otherwise it is skipped. It is not added because a D1 difference looks small. At most the first two formal seeds, four trials.

## Stop rules

Gate 1 or 2 failing, or Gate 4 or 5 failing, stops the formal matrix. Gate 3 failing is recorded; the algorithm is not edited. A continuous cache-pollution failure stops the matrix. The wall budget is 40 minutes from process start. Seeds, lengths, guard, and admission order are not changed after the first result.
