# F6-D1 coverage validation

Label: `OBSERVED`. One four-T4 boot after a scorer correction. Qwen2.5-1.5B-Instruct, vLLM 0.10.2, prefix caching, block size 16, eager, max model length 6144. Prompts are raw token-id lists on `/v1/completions`. No chat template.

The shared prefix is 4096 tokens. The nonce is `F6D1S{seed}{scenario} `, the filler is token 264, and the measured prompt appends 32 tokens of id 293. Length 4128. Every worker in a trial received the same ids. `prompt_sha256` is identical across the four workers of each trial. Different seeds and scenarios differ inside the first 64 ids.

Warmup length is the only difference. A short worker is sent the first 1024 ids. A long worker is sent the first 4096. The measured request is sent later, only to the worker `choose` returned, and is not used as the residency probe.

## What the engine returned

120 warmup rows, 30 trials, including the pilot and D3. All 120 are `BLOCK_CHAIN_MATCH`. Cached tokens on every warmup are 0, which is the cold-prefix omission of `prompt_tokens_details`. Prompt length equals the target. The parent-walk of `BlockStored` token ids matches the warmup prompt. `BlockRemoved` is 0. No `AllBlocksCleared` is ordered after a store.

Each warmup batch begins with one `reset_clear`. vLLM holds that event until the next scheduler step, so it arrives in the warmup batch, before the new stores. A short warmup then has one `BlockStored`. A long warmup has two, 76 of the 120 rows, and the token chain still matches. Those two events are one prefix, not two lineages. There was no eviction to treat as growth.

The measured requests all report prompt length 4128 and four completion tokens. On every valid trial the response `cached_tokens` equals that worker's warmup coverage: 1024 or 4096. The suffix is not cached. The first streamed text is the token piece `unt` on all 30 requests.

## What this does not prove

The reset between worlds is part of the design. This is not a trace of an untouched cache. It also does not show partial eviction. P0-C1's warning still stands: a prefix advertisement can hide a shorter survivor. Nothing here evicted a block, so this round cannot claim the block lifecycle is lossless.

The first pilot, under `raw/pilot_reset_event_window/`, called the deferred reset clear an eviction and stopped before any TTFT. The chain on that pilot already matched. The scorer was corrected before the measured run. Targets, seeds, and `choose` were not changed.
