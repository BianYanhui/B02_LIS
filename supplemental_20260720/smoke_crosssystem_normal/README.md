# Reduced cross-system state-view comparison (item 1, 2026-07-20)

Same backend (4x vLLM 0.10.2 on T4), same native least-loaded policy; only the
state view varies: load_only / sglang_approx (router-side learned map, no
eviction notice, no validation) / preble_global (eviction-aware global view,
locality-first scheduling) / sketch_coverage_k16 (bounded + guard) / exact.
Variants: normal (no capacity pressure) and eviction (physical KV binds).
