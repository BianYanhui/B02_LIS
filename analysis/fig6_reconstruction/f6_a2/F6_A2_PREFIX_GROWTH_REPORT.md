# F6-A2 prefix growth

Label: **OBSERVED** vLLM completions. The prompts are token-id lists, not chat-template strings. The adapter is `EXPERIMENTAL_WRAPPER`.

## Construction

Tokenizer: Qwen2.5-1.5B-Instruct. Each lineage starts with `S{seed}W{worker}N{index:02d} ` (17 tokens on every formal seed) and is padded with the single token id of `" a"` to 4096 ids. Versions are the first 1024, 2048, and 4096 ids. Those lengths are multiples of block size 16. Within a lineage the shorter list is a prefix of the longer list by construction of the slices. Across the 16 lineages of one seed, no nonce is a prefix of another, so one lineage is not a prefix cache of another.

Requests go to `/v1/completions` with `max_tokens=1`. There is no extra residency probe. `cached_tokens` on version 2 and version 3 is the residency evidence for the earlier prefix. `BlockStored` token ids are walked through `parent_block_hash`. Coverage is advertised only when that chain matches the prompt and no `BlockRemoved` or `AllBlocksCleared` occurred in the window.

## Result

| Phase | Seed | Lineages | Versions | Evidence | Coverage | Cached tokens by version | BlockRemoved |
| --- | --- | ---: | ---: | --- | --- | --- | ---: |
| feasibility | 2026101905 | 8 | 24 | `BLOCK_CHAIN_MATCH` on all 24 | 1024, 2048, 4096 | 0, 1024, 2048 | 0 |
| formal | 2026101906, 2026101907, 2026101908 | 16 each, 48 total | 144 | `BLOCK_CHAIN_MATCH` on all 144 | 1024, 2048, 4096 | 0, 1024, 2048 | 0 |

56 lineages, 0 failures of the monotonic coverage check. Version 2 cached exactly the 1024 tokens of version 1. Version 3 cached exactly the 2048 tokens of version 2. The block chain length equals the prompt length and matches the token ids.

Gate 1 and Gate 2 passed before the formal seeds were replayed.

Capture spans: feasibility 6.764 s (offered 3.400 upserts/s); formal 9.899 s, 10.052 s, 10.247 s (4.748, 4.676, 4.587 upserts/s).

This does not show eviction. No block was removed. The result does not say the prefix advertisement is lossless across a full block lifecycle.
