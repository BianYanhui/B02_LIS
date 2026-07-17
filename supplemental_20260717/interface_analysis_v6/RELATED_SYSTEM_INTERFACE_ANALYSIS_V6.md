# Closest-System Interface Analysis (V6)

## Purpose

This artifact answers the novelty-boundary question at the Instance-Dispatcher boundary. It is a source/document analysis, not a benchmark result. Claims marked `not specified` mean the reviewed source did not expose that independent contract; they do not prove the feature is impossible in another implementation.

## Design Delta

Existing cache-aware serving systems optimize placement given integrated or router-learned prefix state. B02's narrow contribution is an explicit bounded prefix-affinity interface: it independently bounds advertised cardinality (K), dissemination traffic (B), and evaluated fanout (J), while treating metadata as a hint that requires owner-side validation. It does not claim globally optimal budget allocation or coverage of batch-global schedulers.

## Direct Baseline Decision

A direct SGLang/Preble latency bar is intentionally absent. On yhs1, neither stack is installed and the server cannot fetch packages. More importantly, an unmodified SGLang router over vLLM backends learns router-side prefix history, whereas B02 evaluates physical vLLM-resident KV visibility and owner validation. Comparing their end-to-end latency without matching cache semantics would not isolate the claimed interface trade-off.

## Cross-Policy Scope

The existing paired replays cover P2C, DualMap-style, and SLO-aware candidate policies. These establish the defined adapter scope: per-request policies that provide a bounded native candidate set and a comparable predicted native cost. They are not evidence for batch-global routing/eviction optimizers or native live SLO results.

## Sources

- Preble, arXiv:2407.00023: https://arxiv.org/abs/2407.00023
- SGLang gateway docs: https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/sgl_model_gateway.md
- SGLang router arguments: https://github.com/sgl-project/sglang/blob/main/sgl-model-gateway/bindings/python/src/sglang_router/router_args.py

See `related_system_interface_matrix_v6.csv`, `real_baseline_feasibility_v6.csv`, and `cross_policy_evidence_v6.csv` for machine-readable rows.
