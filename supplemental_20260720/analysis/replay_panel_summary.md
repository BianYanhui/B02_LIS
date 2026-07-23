# Multi-trace replay panel (2026-07-20)

Rinc = (S_admission - S_load_only) / (S_exact - S_load_only), closed_loop mode.


## nl2bash — agentic_tooluse (paper original)

Exact saved 220718 tokens; Load-Only 149463; affinity margin 71255.

| admission | K=8 Rinc | K=16 Rinc | K=32 Rinc | K=64 Rinc |
|---|---|---|---|---|
| coverage_first | 0.6651 | 0.8147 | 0.9672 | 1.0 |
| lfu | 0.315 | 0.2756 | 0.3035 | 0.4554 |
| lru | 0.3709 | 0.4099 | 0.5206 | 0.6962 |
| oracle_future_value | 0.9071 | 0.9891 | 1.0 | 1.0 |
| reuse_distance | 0.3974 | 0.4087 | 0.4372 | 0.8581 |
| saved_prefill_first | 0.3933 | 0.4088 | 0.4399 | 0.8527 |

## mbpp_s200 — agentic_tooluse (second task)

Exact saved 307223 tokens; Load-Only 186093; affinity margin 121130.

| admission | K=8 Rinc | K=16 Rinc | K=32 Rinc | K=64 Rinc |
|---|---|---|---|---|
| coverage_first | 0.5035 | 0.6974 | 0.9415 | 1.0 |
| lfu | 0.2627 | 0.2789 | 0.3585 | 0.5126 |
| lru | 0.3166 | 0.3538 | 0.4415 | 0.6803 |
| oracle_future_value | 0.8465 | 0.9815 | 1.0 | 1.0 |
| reuse_distance | 0.395 | 0.4325 | 0.4662 | 0.847 |
| saved_prefill_first | 0.3734 | 0.4343 | 0.4787 | 0.8511 |

## sharegpt_s200 — real_chat_multiturn

Exact saved 129650 tokens; Load-Only 116112; affinity margin 13538.

| admission | K=8 Rinc | K=16 Rinc | K=32 Rinc | K=64 Rinc |
|---|---|---|---|---|
| coverage_first | 0.6327 | 0.8814 | 1.0 | 1.0 |
| lfu | 0.2843 | 0.2861 | 0.315 | 0.4301 |
| lru | 0.2768 | 0.3199 | 0.4009 | 0.5779 |
| oracle_future_value | 0.8757 | 0.9888 | 1.0 | 1.0 |
| reuse_distance | 0.3449 | 0.3449 | 0.3674 | 0.9682 |
| saved_prefill_first | 0.3487 | 0.3533 | 0.3623 | 0.9682 |
