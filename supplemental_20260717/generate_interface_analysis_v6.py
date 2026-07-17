#!/usr/bin/env python3
"""Generate auditable closest-system and cross-policy evidence for B02 V6."""
from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path("/home/byh/B02/supplemental_20260715")
OUT = Path("/home/byh/B02/supplemental_20260717/interface_analysis_v6")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def pick(rows: list[dict[str, str]], **filters: str) -> dict[str, str]:
    for row in rows:
        if all(row.get(key) == value for key, value in filters.items()):
            return row
    raise KeyError(filters)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    systems = [
        {
            "system": "B02 Minimal State Sketch (this work)",
            "dispatcher_visible_state": "Per-instance coarse load base plus at most K resident prefix-KV advertisements with digest, scope, epoch, sequence, lease, coverage, and saved-work estimate.",
            "directory_cardinality_contract": "Explicit: <= N*K advertised entries.",
            "traffic_contract": "Explicit: per-instance token bucket, coalescing, and tombstones.",
            "candidate_fanout_contract": "Explicit: union is ranked then evaluated candidates are truncated to J.",
            "stale_hint_handling": "Owner-side ValidateAndPin; V6 uses live vLLM BlockPool.touch/free_blocks in EngineCore's serialized loop.",
            "policy_binding": "Candidate-based per-request adapters; P2C, DualMap-style, and SLO-aware replay are evaluated.",
            "comparison_interpretation": "Mechanism supplies independent cardinality, traffic, and fanout contracts; it is not claimed to solve globally optimal state allocation.",
            "source": "B02 V6 patch and experiments (2026-07-17).",
            "source_url": "local:B02/supplemental_20260717/vllm_0.10.2_b02_native_pin.patch",
        },
        {
            "system": "Preble (NSDI 2025)",
            "dispatcher_visible_state": "Integrated global scheduler uses prompt-sharing/prefix-tree state and load/eviction cost estimates across GPU instances.",
            "directory_cardinality_contract": "No independent per-instance top-K visibility contract specified in the paper.",
            "traffic_contract": "No separately exposed dispatcher-dissemination byte-rate contract specified in the paper.",
            "candidate_fanout_contract": "No J-style per-request affinity candidate cap specified in the paper.",
            "stale_hint_handling": "Integrated scheduler/runtime; a separate stale-advertisement versus owner-capability contract is not specified in the paper.",
            "policy_binding": "Tightly integrated global + local scheduling, including prompt sharing, eviction cost, load and prefill/decode balancing.",
            "comparison_interpretation": "Closest integrated distributed prompt scheduler; not a drop-in router plugin over the four vLLM backends.",
            "source": "Preble paper, sections 3-4, arXiv:2407.00023 (accessed 2026-07-17).",
            "source_url": "https://arxiv.org/abs/2407.00023",
        },
        {
            "system": "SGLang Router / SGL Model Gateway", 
            "dispatcher_visible_state": "Router-side cache-aware radix/approximation tree plus worker load state; state is learned by the router from request routing.",
            "directory_cardinality_contract": "--max-tree-size bounds the approximation tree, not an advertised per-instance prefix-K interface.",
            "traffic_contract": "Configurable eviction cadence is documented; no independent per-worker metadata byte-rate budget is documented.",
            "candidate_fanout_contract": "No J-style evaluated-affinity candidate bound documented in the router arguments.",
            "stale_hint_handling": "Docs note replica radix trees are not synchronized; a runtime-native owner ValidateAndPin capability contract is not documented.",
            "policy_binding": "Supports cache_aware, power_of_two, consistent_hashing, prefix_hash, and other router policies.",
            "comparison_interpretation": "Closest current router. Its learned router tree and B02 physical-residency visibility are different measurement semantics, so a direct latency comparison over unchanged vLLM backends would be confounded.",
            "source": "SGLang gateway docs and router arguments (accessed 2026-07-17).",
            "source_url": "https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/sgl_model_gateway.md | https://github.com/sgl-project/sglang/blob/main/sgl-model-gateway/bindings/python/src/sglang_router/router_args.py",
        },
        {
            "system": "vLLM automatic prefix caching", 
            "dispatcher_visible_state": "Runtime-local cached KV blocks; no built-in distributed dispatcher directory is exposed by this deployment.",
            "directory_cardinality_contract": "No cross-instance dispatcher directory.",
            "traffic_contract": "No cross-instance affinity dissemination path.",
            "candidate_fanout_contract": "Not applicable without an external router.",
            "stale_hint_handling": "Runtime-local block reference accounting; B02 V6 exercises this BlockPool in an experimental owner API.",
            "policy_binding": "Local runtime cache; external request placement is outside automatic prefix caching.",
            "comparison_interpretation": "Serving-runtime substrate, not a distributed cache-aware router baseline.",
            "source": "vLLM 0.10.2 source deployed on yhs1 and B02 V6 runtime patch.",
            "source_url": "local:/home/byh/B02/poc/.venv/lib/python3.12/site-packages/vllm/v1/core/block_pool.py",
        },
    ]
    write_csv(OUT / "related_system_interface_matrix_v6.csv", systems)

    feasibility = [
        {
            "candidate": "SGLang Router cache_aware over four existing vLLM HTTP backends",
            "T4_hardware_sufficient": "Yes for Qwen2.5-1.5B four-worker functional tests.",
            "software_status_on_yhs1": "Not installed; yhs1 has no route to download the package and no cached artifact was found.",
            "semantic_comparability": "Insufficient for a headline B02 comparison: router learns an approximate radix tree, whereas B02 Exact/Sketch tests use known vLLM resident-state metadata and owner validation.",
            "decision": "Do not synthesize a direct latency baseline. Retain the source-level interface comparison and label any future port as a separate, explicitly approximate-routing experiment.",
            "status": "NotRun_NotFairAndUnavailable",
        },
        {
            "candidate": "Preble against the current vLLM endpoints",
            "T4_hardware_sufficient": "Potentially for a reduced Qwen functional build, but not the blocker.",
            "software_status_on_yhs1": "Not installed; Preble is an integrated global/local scheduling stack rather than a drop-in vLLM HTTP policy module.",
            "semantic_comparability": "Insufficient without porting its runtime and matching eviction/scheduler behavior; otherwise it compares whole serving stacks rather than state interfaces.",
            "decision": "Do not report a pseudo-baseline. Use it as closest-work analysis; a faithful replication needs a separate environment and engineering effort.",
            "status": "NotRun_IncompatibleRuntime",
        },
    ]
    write_csv(OUT / "real_baseline_feasibility_v6.csv", feasibility)

    cross = read_csv(ROOT / "cross_policy_v4" / "cross_policy_summary.csv")
    slo = read_csv(ROOT / "slo_aware_replay_v5" / "slo_aware_summary.csv")
    p2c16 = pick(cross, policy_family="p2c", interface="sketch_coverage", K="16")
    dual16 = pick(cross, policy_family="dualmap", interface="sketch_coverage", K="16")
    slo_steady = pick(slo, load_regime="steady", interface="slo_sketch_coverage", K="32")
    slo_burst = pick(slo, load_regime="burst", interface="slo_sketch_coverage", K="32")
    policy_evidence = [
        {
            "policy_family": "P2C load-driven candidate policy",
            "interface": "Sketch K=16 versus Exact",
            "evidence_type": p2c16["evidence_type"],
            "repetitions": p2c16["n_reps"],
            "exact_normalized_incremental_saved_prefill": p2c16["incremental_saved_vs_exact_ratio_mean"],
            "sketch_index_bytes": p2c16["dispatcher_index_bytes_mean"],
            "writing_scope": "Supports candidate-based adapter generality under paired trace-derived replay; not a live throughput claim.",
            "source_dataset": "cross_policy_v4",
        },
        {
            "policy_family": "DualMap-style stable-hash candidate policy",
            "interface": "Sketch K=16 versus Exact",
            "evidence_type": dual16["evidence_type"],
            "repetitions": dual16["n_reps"],
            "exact_normalized_incremental_saved_prefill": dual16["incremental_saved_vs_exact_ratio_mean"],
            "sketch_index_bytes": dual16["dispatcher_index_bytes_mean"],
            "writing_scope": "Supports bounded visibility with a locality-preserving native policy; report that its native locality reduces the marginal affinity gain.",
            "source_dataset": "cross_policy_v4",
        },
        {
            "policy_family": "SLO-aware abstaining candidate policy (steady)",
            "interface": "Sketch K=32 versus Exact",
            "evidence_type": slo_steady["evidence_type"],
            "repetitions": slo_steady["n_reps"],
            "exact_normalized_incremental_saved_prefill": slo_steady["incremental_saved_vs_exact_ratio_mean"],
            "sketch_index_bytes": slo_steady["dispatcher_index_bytes_mean"],
            "writing_scope": "Supports deadline-aware guard integration in paired dispatcher replay; TTFT/SLO values are modeled, not live serving values.",
            "source_dataset": "slo_aware_replay_v5",
        },
        {
            "policy_family": "SLO-aware abstaining candidate policy (burst)",
            "interface": "Sketch K=32 versus Exact",
            "evidence_type": slo_burst["evidence_type"],
            "repetitions": slo_burst["n_reps"],
            "exact_normalized_incremental_saved_prefill": slo_burst["incremental_saved_vs_exact_ratio_mean"],
            "sketch_index_bytes": slo_burst["dispatcher_index_bytes_mean"],
            "writing_scope": "Supports stress sensitivity, but retain confidence intervals and do not claim universal token-bucket or SLO dominance.",
            "source_dataset": "slo_aware_replay_v5",
        },
    ]
    write_csv(OUT / "cross_policy_evidence_v6.csv", policy_evidence)

    lines = [
        "# Closest-System Interface Analysis (V6)", "",
        "## Purpose", "",
        "This artifact answers the novelty-boundary question at the Instance-Dispatcher boundary. It is a source/document analysis, not a benchmark result. Claims marked `not specified` mean the reviewed source did not expose that independent contract; they do not prove the feature is impossible in another implementation.", "",
        "## Design Delta", "",
        "Existing cache-aware serving systems optimize placement given integrated or router-learned prefix state. B02's narrow contribution is an explicit bounded prefix-affinity interface: it independently bounds advertised cardinality (K), dissemination traffic (B), and evaluated fanout (J), while treating metadata as a hint that requires owner-side validation. It does not claim globally optimal budget allocation or coverage of batch-global schedulers.", "",
        "## Direct Baseline Decision", "",
        "A direct SGLang/Preble latency bar is intentionally absent. On yhs1, neither stack is installed and the server cannot fetch packages. More importantly, an unmodified SGLang router over vLLM backends learns router-side prefix history, whereas B02 evaluates physical vLLM-resident KV visibility and owner validation. Comparing their end-to-end latency without matching cache semantics would not isolate the claimed interface trade-off.", "",
        "## Cross-Policy Scope", "",
        "The existing paired replays cover P2C, DualMap-style, and SLO-aware candidate policies. These establish the defined adapter scope: per-request policies that provide a bounded native candidate set and a comparable predicted native cost. They are not evidence for batch-global routing/eviction optimizers or native live SLO results.", "",
        "## Sources", "",
        "- Preble, arXiv:2407.00023: https://arxiv.org/abs/2407.00023", 
        "- SGLang gateway docs: https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/sgl_model_gateway.md", 
        "- SGLang router arguments: https://github.com/sgl-project/sglang/blob/main/sgl-model-gateway/bindings/python/src/sglang_router/router_args.py", 
        "",
        "See `related_system_interface_matrix_v6.csv`, `real_baseline_feasibility_v6.csv`, and `cross_policy_evidence_v6.csv` for machine-readable rows.",
    ]
    (OUT / "RELATED_SYSTEM_INTERFACE_ANALYSIS_V6.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
