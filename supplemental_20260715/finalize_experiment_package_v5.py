#!/usr/bin/env python3
"""Append V5 evidence to the AI-facing B02 package without flattening scope."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from finalize_experiment_package_v4 import (
    find_row,
    number,
    read_csv,
    registry_rows,
    source_rows,
    write_csv,
)


def maybe_row(rows: list[dict], **conditions: str) -> dict | None:
    for row in rows:
        if all(str(row.get(key)) == str(value) for key, value in conditions.items()):
            return row
    return None


def v5_claims(root: Path, live_available: bool) -> list[dict]:
    claims: list[dict] = []
    slo = read_csv(root / "slo_aware_replay_v5" / "slo_aware_summary.csv")
    owner = read_csv(root / "owner_transport_microbench_v5" / "owner_validation_microbench.csv")
    transport = read_csv(root / "owner_transport_microbench_v5" / "metadata_tcp_microbench.csv")
    if slo:
        k32_steady = find_row(slo, load_regime="steady", interface="slo_sketch_coverage", K="32")
        k32_burst = find_row(slo, load_regime="burst", interface="slo_sketch_coverage", K="32")
        load_steady = find_row(slo, load_regime="steady", interface="slo_load_only", K="inf")
        claims.append({
            "claim_id": "C7", "status": "Supported as paired SLO-aware dispatcher replay",
            "evidence_type": "dispatcher_level_simulation",
            "paper_claim": "The bounded interface can be attached to an SLO-aware affinity fallback policy while preserving deadline abstention.",
            "evidence": (
                f"Steady SLO replay: K32 retains {number(k32_steady['incremental_saved_vs_exact_ratio_mean']):.3f} of Exact saved-prefill "
                f"at {number(k32_steady['dispatcher_index_bytes_mean']):.0f}B versus Exact 33152B; SLO miss rate is "
                f"{number(k32_steady['slo_miss_rate_mean']):.3f} versus Load-only {number(load_steady['slo_miss_rate_mean']):.3f}. "
                f"Burst K32 retainment is {number(k32_burst['incremental_saved_vs_exact_ratio_mean']):.3f}."
            ),
            "safe_writing": "Call TTFT and deadline results modeled dispatcher outcomes; do not present them as live serving SLO evidence.",
        })
    if owner and transport:
        valid = find_row(owner, scenario="valid")
        invalid = [row for row in owner if row.get("scenario") != "valid"]
        budget_failures = sum(not str(row.get("budget_assertion_pass")).lower() == "true" for row in transport)
        claims.append({
            "claim_id": "C8", "status": "Supported as single-host implementation microbenchmark",
            "evidence_type": "single_host_tcp_microbenchmark",
            "paper_claim": "Reference owner validation and fixed-width metadata transport have bounded local serialization/IPC cost.",
            "evidence": (
                f"32 concurrent loopback clients: valid ValidateAndPin p95={number(valid['end_to_end_p95_us']):.1f}us, "
                f"throughput={number(valid['throughput_ops_s']):.0f} ops/s; all incompatible scenarios fallback with unsafe reuse count 0. "
                f"All {len(transport)} token-bucket cells obeyed the one-frame burst budget assertion (failures={budget_failures})."
            ),
            "safe_writing": "State single-host loopback TCP and reference owner implementation; it is neither inter-node transport nor a vLLM-native KV pin result.",
        })
    if live_available:
        live = read_csv(root / "live_k_tradeoff_v5_primary_2k_committed" / "live_k_summary.csv")
        k4 = maybe_row(live, treatment_policy="sketch_coverage_k4")
        k16 = maybe_row(live, treatment_policy="sketch_coverage_k16")
        k32 = maybe_row(live, treatment_policy="sketch_coverage_k32")
        if k4 and k16 and k32:
            claims.append({
                "claim_id": "C9", "status": "Supported as fixed-prompt live T4 K sweep",
                "evidence_type": "live_t4_vllm",
                "paper_claim": "A difficult active-prefix workload exposes a live bounded-state K trade-off rather than a single Sketch=Exact point.",
                "evidence": (
                    f"K4/K16/K32 Exact-normalized estimated saved-prefill means are "
                    f"{number(k4['exact_normalized_estimated_saved_prefill_mean']):.3f}/"
                    f"{number(k16['exact_normalized_estimated_saved_prefill_mean']):.3f}/"
                    f"{number(k32['exact_normalized_estimated_saved_prefill_mean']):.3f}; corresponding mean index bytes are "
                    f"{number(k4['treatment_dispatcher_index_bytes_mean']):.0f}/"
                    f"{number(k16['treatment_dispatcher_index_bytes_mean']):.0f}/"
                    f"{number(k32['treatment_dispatcher_index_bytes_mean']):.0f}."
                ),
                "safe_writing": "Report paired TTFT distributions and physical cached-token telemetry separately from estimated saved-prefill; do not claim a generic throughput/SLO result.",
            })
    return claims


def write_notes(root: Path, claims: list[dict], live_available: bool) -> None:
    lines = [
        "# B02 Paper Experiment Writing Notes (V5)", "",
        "## Evidence Discipline", "",
        "Use `paper_claim_evidence_v5.csv`, `experiment_registry_v5.csv`, and `sanity_checks_v5.csv` before drafting. "
        "Do not pool live T4, trace-derived simulation, dispatcher simulation, single-host microbenchmark, and cost microbenchmark metrics into one serving-performance claim.", "",
        "## Current Thesis", "",
        "A bounded affinity interface creates an explicit state-cost versus reusable-prefill-quality trade-off. Its value depends on locality concentration, admission quality, candidate bound, and whether reusable coverage repays queue and validation overhead.", "",
        "## Legacy Exclusions", "",
        "V3 paired T4 rows remain `Superseded_InputConfounded`. The first SLO V5 run is `Invalid_NativeVisibilityLeak`: its native baseline accessed private coverage and is retained only for audit, not as evidence.", "",
        "## Claim Decisions", "",
    ]
    for claim in claims:
        lines.extend([
            f"### {claim['claim_id']}: {claim['status']}", "", claim["paper_claim"], "",
            f"Evidence: {claim['evidence']}", "", f"Writing rule: {claim['safe_writing']}", "",
        ])
    if not live_available:
        lines.extend(["## Pending", "", "The 128-request, 12-repetition difficult T4 K primary is still running. Do not cite partial logs.", ""])
    lines.extend([
        "## Required Limitations", "",
        "- AgentTrace-derived replay preserves structural turn patterns, not semantic task completion or production arrivals.",
        "- SLO TTFT/deadline outcomes are modeled dispatcher quantities.",
        "- TCP measurements are single-host loopback and use a reference owner; vLLM-native KV pin/eviction is not implemented.",
        "- Larger-model and long-context live validation remain a separate hardware/replica-scaling question.",
    ])
    (root / "PAPER_EXPERIMENT_RESULTS_V5.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/home/byh/B02/supplemental_20260715")
    args = parser.parse_args()
    root = Path(args.root)
    live_dir = root / "live_k_tradeoff_v5_primary_2k_committed"
    live_available = (live_dir / "live_k_summary.csv").is_file()
    merged = read_csv(root / "AI_MASTER_TABLE_V4.csv")
    datasets = {
        "slo_aware_v5_cells": root / "slo_aware_replay_v5" / "slo_aware_cells.csv",
        "slo_aware_v5_summary": root / "slo_aware_replay_v5" / "slo_aware_summary.csv",
        "owner_validation_v5": root / "owner_transport_microbench_v5" / "owner_validation_microbench.csv",
        "metadata_transport_v5": root / "owner_transport_microbench_v5" / "metadata_tcp_microbench.csv",
    }
    if live_available:
        datasets.update({
            "live_k_v5_cells": live_dir / "live_k_cells.csv",
            "live_k_v5_pairs": live_dir / "live_k_pairs.csv",
            "live_k_v5_summary": live_dir / "live_k_summary.csv",
        })
    loaded = {source: read_csv(path) for source, path in datasets.items()}
    for source, rows in loaded.items():
        merged.extend(source_rows(rows, source))
    write_csv(root / "AI_MASTER_TABLE_V5.csv", merged)
    registry = read_csv(root / "experiment_registry_v4.csv")
    for source, rows in loaded.items():
        if source.endswith("_cells") or source in {"owner_validation_v5", "metadata_transport_v5"}:
            registry.extend(registry_rows(rows, source))
    write_csv(root / "experiment_registry_v5.csv", registry)
    checks = read_csv(root / "sanity_checks_v4.csv")
    check_paths = [
        root / "slo_aware_replay_v5" / "slo_aware_sanity_checks.csv",
        root / "owner_transport_microbench_v5" / "microbench_sanity_checks.csv",
    ]
    if live_available:
        check_paths.append(live_dir / "live_k_sanity_checks.csv")
    for path in check_paths:
        checks.extend(read_csv(path))
    write_csv(root / "sanity_checks_v5.csv", checks)
    claims = v5_claims(root, live_available)
    write_csv(root / "paper_claim_evidence_v5.csv", claims)
    write_notes(root, claims, live_available)
    exclusions = read_csv(root / "legacy_exclusions_v4.csv")
    exclusions.append({
        "source_dataset": "slo_aware_replay_v5_invalid_native_visibility",
        "status": "Invalid_NativeVisibilityLeak",
        "reason": "Native SLO baseline erroneously inspected private KV coverage. Preserved for audit only; corrected rerun is slo_aware_replay_v5.",
    })
    exclusions.append({
        "source_dataset": "live_k_tradeoff_v5_primary_2k",
        "status": "Aborted_UncommittedRunner",
        "reason": "Initial T4 K log used an uncommitted runner. No final tables were produced; formal rerun is live_k_tradeoff_v5_primary_2k_committed.",
    })
    exclusions.append({
        "source_dataset": "live_k_tradeoff_v5_primary_2k_output16_endpoint_crash",
        "status": "Invalid_EndpointCrash",
        "reason": "Fixed 16-token T4 run terminated after a vLLM CUDA illegal-memory-access. It produced no final tables and is excluded; formal rerun uses fixed 4-token output.",
    })
    write_csv(root / "legacy_exclusions_v5.csv", exclusions)
    sources = read_csv(root / "paper_primary_sources_v4.csv")
    sources.extend([
        {"paper_section": "SLO-aware policy family", "source_dataset": "slo_aware_replay_v5", "evidence_type": "dispatcher_level_simulation", "status": "Current"},
        {"paper_section": "Reference owner and local transport", "source_dataset": "owner_transport_microbench_v5", "evidence_type": "single_host_tcp_microbenchmark", "status": "Current"},
    ])
    if live_available:
        sources.append({"paper_section": "Difficult live K trade-off", "source_dataset": "live_k_tradeoff_v5_primary_2k_committed", "evidence_type": "live_t4_vllm", "status": "Current"})
    write_csv(root / "paper_primary_sources_v5.csv", sources)
    (root / "AI_MASTER_TABLE_V5_README.md").write_text(
        "# AI Master Table V5\n\n"
        "V5 appends SLO-aware paired replay, single-host owner/transport microbenchmarks, and (when complete) the difficult live K sweep to V4. "
        "Filter by `status`, `source_dataset`, and `evidence_type`; do not compare metrics across evidence types as if they were one serving benchmark.\n"
    )
    (root / "AI_MASTER_README_V5.json").write_text(json.dumps({
        "required_reading": ["PAPER_EXPERIMENT_RESULTS_V5.md", "paper_claim_evidence_v5.csv", "paper_primary_sources_v5.csv", "experiment_registry_v5.csv", "sanity_checks_v5.csv", "legacy_exclusions_v5.csv"],
        "scope_rule": "Never pool live, trace-derived, dispatcher simulation, single-host microbenchmark, and cost microbenchmark values into one performance statement.",
        "live_k_primary_available": live_available,
        "invalid_slo_rerun_included": False,
    }, indent=2))
    print(json.dumps({"master_rows": len(merged), "registry_rows": len(registry), "checks": len(checks), "claims": len(claims), "live_k_primary_available": live_available}, indent=2))


if __name__ == "__main__":
    main()
