# vLLM-native ValidateAndPin microbenchmark (V6)

This is a live vLLM 0.10.2 runtime microbenchmark on one Tesla T4. The B02 developer endpoint validates scope/version/lease, rescans live prefix-cache blocks, and calls `BlockPool.touch()` in the serialized EngineCore utility loop; release calls `BlockPool.free_blocks()`. The test injects stale fields, synthetic owner epoch advance, actual BlockPool eviction, and concurrent eviction attempts while pinned. It measures owner-side runtime operations, not end-to-end dispatch or multi-node transport.
