#!/usr/bin/env python3
"""Merge V6 vLLM-native evidence with the audited B02 V5 package."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


BASE = Path("/home/byh/B02/supplemental_20260715")
V6 = Path("/home/byh/B02/supplemental_20260717")
OUT = V6 / "merged_experiment_package_v6"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(value: str | int | float | None) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def row(rows: list[dict[str, str]], scenario: str) -> dict[str, str]:
    for item in rows:
        if item.get("scenario") == scenario:
            return item
    raise KeyError(scenario)


def source_rows(rows: list[dict[str, str]], source: str) -> list[dict[str, str]]:
    return [{"source_dataset": source, "status": item.get("status", "Current"),
             "evidence_type": item.get("evidence_type", "unknown"), **item}
            for item in rows]


def native_registry(rows: list[dict[str, str]], metadata: dict[str, Any]) -> list[dict[str, str]]:
    return [{
        "experiment_id": f"20260717_vllm_native_{item['scenario']}",
        "evidence_type": item["evidence_type"],
        "code_commit": item["code_commit"], "model": item["model"],
        "hardware": "yhs1 GPU0: Tesla T4 15GB; single vLLM 0.10.2 endpoint; loopback HTTP developer API",
        "N": "1", "K": "n/a", "J": "n/a", "rate_budget_Bps": "n/a",
        "workload_trace_hash": metadata["prefix_sha256"], "seed": "17",
        "repetitions": item["operations"],
        "prefix_length_distribution": f"fixed {item['prefix_tokens']}-token prompt; {item['advertised_coverage_tokens']} cached full-block coverage",
        "locality": "one identical resident prefix", "cache_capacity": "vLLM reported 349984 KV tokens at startup",
        "eviction_policy": "vLLM BlockPool cached-block eviction; test injection uses BlockPool._maybe_evict_cached_block",
        "status": "Current", "source_sheet": "vllm_native_validation_v6",
        "supersedes": "owner_transport_microbench_v5 only for runtime-native validation claim",
        "supersession_reason": "V6 calls live vLLM BlockPool.touch/free_blocks in EngineCore; V5 remains current for fixed-frame loopback transport only.",
    } for item in rows]


def write_paper_inserts(native: list[dict[str, str]], policy: list[dict[str, str]]) -> None:
    valid = row(native, "valid_parallel")
    race = row(native, "concurrent_evict_while_pinned")
    invalid_ops = sum(int(item["operations"]) for item in native
                      if item["scenario"] not in {"valid_parallel", "concurrent_evict_while_pinned"})
    p2c = next(item for item in policy if item["policy_family"].startswith("P2C"))
    dual = next(item for item in policy if item["policy_family"].startswith("DualMap"))
    steady = next(item for item in policy if "steady" in item["policy_family"])
    lines = [
        "# Paper-Ready Evidence and Wording (V6)", "",
        "## Recommended Scope", "",
        "Use the narrow claim: **B02 exposes a bounded resident prefix-KV affinity interface with explicit directory, traffic, and evaluated-fanout bounds; its quality depends on locality and admission, and stale advertisements remain hints rather than reuse capabilities.**", "",
        "Do not claim universal latency improvement, globally optimal budget allocation, arbitrary reusable-state semantics, universal policy support, or a direct performance win over SGLang/Preble.", "",
        "## Runtime-Native Correctness Paragraph", "",
        f"We integrated the owner operation with vLLM 0.10.2's live `BlockPool`: `ValidateAndPin` checks the advertised digest, tenant, model revision, epoch, sequence, lease, and current cache coverage, then invokes `BlockPool.touch()` in the EngineCore utility loop; release invokes `BlockPool.free_blocks()` on the same runtime. On one Tesla T4 with a {valid['prefix_tokens']}-token prompt ({valid['advertised_coverage_tokens']} full cached tokens), {valid['operations']} valid validations from {valid['concurrent_clients']} concurrent HTTP clients all pinned and released successfully (owner-operation p50/p95/p99 {number(valid['validate_p50_us']) / 1000:.1f}/{number(valid['validate_p95_us']) / 1000:.1f}/{number(valid['validate_p99_us']) / 1000:.1f} ms; {number(valid['throughput_ops_s']):.0f} operations/s). Across {invalid_ops} injected stale cases covering epoch, sequence, tenant, model revision, lease expiry, physical eviction, and restart epoch, unsafe reuse was zero. During {race['eviction_attempts']} concurrent eviction attempts, every eviction was rejected while the prefix was pinned; after the cancellation-release branch, the prefix became evictable.", "",
        "Writing constraint: call these **single-host runtime-owner operations over loopback HTTP**. The measurement includes the developer API and utility-channel overhead, not only a critical-section CPU cost. The cancellation test exercises the release branch; it is not yet a transparent hook on every OpenAI client disconnect path.", "",
        "## Cross-Policy Paragraph", "",
        f"For the candidate-based adapter scope, paired trace-derived replay shows that Sketch-K16 retains {number(p2c['exact_normalized_incremental_saved_prefill']):.3f} of Exact's incremental saved-prefill under P2C and {number(dual['exact_normalized_incremental_saved_prefill']):.3f} under DualMap-style routing, each with a {number(p2c['sketch_index_bytes']):.0f}-byte dispatcher index in the reported setup. For a deadline-abstaining SLO-aware candidate policy, steady-state Sketch-K32 retains {number(steady['exact_normalized_incremental_saved_prefill']):.3f} of Exact. These are replay/model outcomes, not live end-to-end SLO measurements.", "",
        "## Closest-System Positioning", "",
        "Preble is the closest integrated distributed prompt scheduler; SGLang Router is the closest current router-side cache-aware system. The paper should compare their state boundaries rather than assert a direct unrun latency comparison. B02's defensible delta is independent contracts for per-instance advertisement cardinality K, metadata dissemination B, evaluated fanout J, and owner-validated stale hints. Use `related_system_interface_matrix_v6.csv` as the source for a compact related-system table.", "",
        "## Mandatory Limitations", "",
        "- The native integration is a B02 experimental vLLM 0.10.2 developer API on one host, not a production upstream API or multi-node deployment.",
        "- The cross-policy adapter targets per-request candidate-based policies; batch-global schedulers and joint routing/eviction optimizers remain out of scope.",
        "- SGLang Router and Preble are not directly benchmarked here because they are unavailable on yhs1 and do not share B02's physical-KV state semantics; a direct latency bar would confound whole-stack differences.",
        "- Existing live latency evidence remains scoped as in V5; do not use it to claim a universal TTFT reduction.", "",
    ]
    (OUT / "V6_PAPER_INSERTS.md").write_text("\n".join(lines))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    native_dir = V6 / "vllm_native_pin_v6"
    native = read_csv(native_dir / "vllm_native_validation_microbench.csv")
    native_checks = read_csv(native_dir / "vllm_native_validation_sanity_checks.csv")
    metadata = json.loads((native_dir / "run_metadata.json").read_text())
    policy = read_csv(V6 / "interface_analysis_v6" / "cross_policy_evidence_v6.csv")

    master = read_csv(BASE / "AI_MASTER_TABLE_V5.csv")
    master.extend(source_rows(native, "vllm_native_validation_v6"))
    write_csv(OUT / "AI_MASTER_TABLE_V6.csv", master)

    registry = read_csv(BASE / "experiment_registry_v5.csv")
    registry.extend(native_registry(native, metadata))
    write_csv(OUT / "experiment_registry_v6.csv", registry)

    checks = read_csv(BASE / "sanity_checks_v5.csv")
    checks.extend([{**item, "source_dataset": "vllm_native_validation_v6"}
                   for item in native_checks])
    write_csv(OUT / "sanity_checks_v6.csv", checks)

    claims = read_csv(BASE / "paper_claim_evidence_v5.csv")
    valid = row(native, "valid_parallel")
    race = row(native, "concurrent_evict_while_pinned")
    claims.extend([
        {
            "claim_id": "C10", "status": "Supported as single-host vLLM runtime integration",
            "evidence_type": "live_vllm_runtime_microbenchmark",
            "paper_claim": "Owner-side validation can pin and release the actual vLLM KV-cache blocks while stale metadata remains non-authoritative.",
            "evidence": f"V6: {valid['operations']} valid pins/releases at {valid['concurrent_clients']} clients (p95 {number(valid['validate_p95_us']) / 1000:.1f} ms); all stale injections fallback with unsafe reuse 0; {race['blocked_evictions']}/{race['eviction_attempts']} eviction attempts were blocked while pinned and post-cancellation release eviction succeeded.",
            "safe_writing": "State a single-host B02 vLLM 0.10.2 developer-endpoint integration with loopback HTTP. Do not claim a production upstream API, multi-node transport, or automatic release on all client disconnects.",
        },
        {
            "claim_id": "C11", "status": "Supported as source/document interface analysis",
            "evidence_type": "closest-system_interface_analysis",
            "paper_claim": "The contribution is a bounded prefix-affinity interface contract, not a replacement for integrated whole-stack cache-aware schedulers.",
            "evidence": "V6 matrix compares B02 against Preble, SGLang Router, and vLLM local prefix caching by visible state, K/B/J contracts, stale handling, and policy binding.",
            "safe_writing": "Use the matrix for related-work positioning only. It is not a performance benchmark or a proof that no implementation has similar features.",
        },
    ])
    write_csv(OUT / "paper_claim_evidence_v6.csv", claims)

    sources = read_csv(BASE / "paper_primary_sources_v5.csv")
    sources.extend([
        {"paper_section": "Runtime-native validation", "source_dataset": "vllm_native_validation_v6", "evidence_type": "live_vllm_runtime_microbenchmark", "status": "Current"},
        {"paper_section": "Closest-system boundary", "source_dataset": "interface_analysis_v6", "evidence_type": "source_document_analysis", "status": "Current"},
    ])
    write_csv(OUT / "paper_primary_sources_v6.csv", sources)

    lineage = read_csv(BASE / "legacy_exclusions_v5.csv")
    lineage.append({
        "source_dataset": "owner_transport_microbench_v5",
        "status": "Current_TransportOnly",
        "reason": "V5 remains the fixed-frame loopback metadata transport evidence. V6 supersedes it only for the runtime-native owner ValidateAndPin claim.",
    })
    write_csv(OUT / "legacy_exclusions_v6.csv", lineage)

    for filename in ("related_system_interface_matrix_v6.csv", "real_baseline_feasibility_v6.csv",
                     "cross_policy_evidence_v6.csv", "RELATED_SYSTEM_INTERFACE_ANALYSIS_V6.md"):
        (OUT / filename).write_bytes((V6 / "interface_analysis_v6" / filename).read_bytes())
    for filename in ("vllm_native_validation_microbench.csv", "vllm_native_validation_sanity_checks.csv",
                     "run_metadata.json", "README.md"):
        (OUT / f"native_{filename}").write_bytes((native_dir / filename).read_bytes())
    write_paper_inserts(native, policy)

    (OUT / "AI_MASTER_TABLE_V6_README.md").write_text(
        "# B02 AI Master Table V6\n\n"
        "V6 preserves all V5 rows and appends the live vLLM runtime-owner microbenchmark. "
        "Filter by `status`, `source_dataset`, and `evidence_type`; do not pool trace simulations, live serving, runtime microbenchmarks, and control-plane microbenchmarks. "
        "Read `V6_PAPER_INSERTS.md`, `paper_claim_evidence_v6.csv`, `sanity_checks_v6.csv`, and `related_system_interface_matrix_v6.csv` before drafting.\n"
    )
    (OUT / "AI_MASTER_README_V6.json").write_text(json.dumps({
        "required_reading": ["V6_PAPER_INSERTS.md", "paper_claim_evidence_v6.csv", "sanity_checks_v6.csv", "experiment_registry_v6.csv", "related_system_interface_matrix_v6.csv", "real_baseline_feasibility_v6.csv"],
        "evidence_separation_rule": "Never combine live runtime-owner latency, live T4 serving latency, trace-derived replay, dispatcher simulation, or control-plane microbenchmark metrics into one performance statistic.",
        "native_runtime_scope": "single-host B02 vLLM 0.10.2 developer API; BlockPool touch/free lifecycle verified; not a production multi-node API.",
        "master_rows": len(master), "registry_rows": len(registry), "sanity_rows": len(checks),
    }, indent=2))
    print(json.dumps({"master_rows": len(master), "registry_rows": len(registry),
                      "sanity_rows": len(checks), "claims": len(claims),
                      "out": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
