#!/usr/bin/env python3
"""Build an AI-facing B02 V4 evidence package without erasing prior lineage."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_row(rows: list[dict], **conditions: str) -> dict:
    for row in rows:
        if all(str(row.get(key)) == str(value) for key, value in conditions.items()):
            return row
    raise KeyError(f"missing row: {conditions}")


def source_rows(rows: list[dict], source: str, default_status: str = "Current") -> list[dict]:
    output = []
    for row in rows:
        normalized = {"source_dataset": source, "status": row.get("status", default_status), "evidence_type": row.get("evidence_type", "simulation"), **row}
        output.append(normalized)
    return output


def registry_rows(rows: list[dict], source: str) -> list[dict]:
    output = []
    for index, row in enumerate(rows):
        output.append({
            "experiment_id": row.get("experiment_id", f"20260716_{source}_{index:05d}"),
            "evidence_type": row.get("evidence_type", "simulation"),
            "code_commit": row.get("code_commit", ""),
            "model": row.get("model", ""),
            "hardware": row.get("hardware", ""),
            "N": row.get("N", row.get("n_instances", "")),
            "K": row.get("K", ""), "J": row.get("J", ""),
            "rate_budget_Bps": row.get("rate_budget_Bps", ""),
            "workload_trace_hash": row.get("workload_trace_hash", row.get("source_file_sha256", "")),
            "seed": row.get("seed", ""), "repetitions": row.get("repetitions", 1),
            "prefix_length_distribution": row.get("prefix_length_distribution", row.get("prefix_token_target", "experiment-specific")),
            "locality": row.get("locality", "n/a"), "cache_capacity": row.get("cache_capacity", "n/a"),
            "eviction_policy": row.get("eviction_policy", "bounded LRU / experiment-specific"),
            "status": row.get("status", "Current"), "source_sheet": source,
            "supersedes": row.get("supersedes", ""), "supersession_reason": row.get("supersession_reason", ""),
        })
    return output


def mark_lineage(rows: list[dict]) -> list[dict]:
    output = []
    for row in rows:
        current = dict(row)
        source = str(current.get("source_dataset", ""))
        if source.startswith("paired_t4_latency_v3"):
            current["status"] = "Superseded_InputConfounded"
            current["superseded_by"] = "fixed_prompt_t4_v4_primary"
            current["supersession_reason"] = "V3 embedded policy namespace in semantic prompt, changing model input/output; V4 uses cache_salt outside prompt and fixed output length."
        elif source.startswith("agenttrace_structural"):
            current["status"] = "Legacy_DiagnosticNegative"
            current["superseded_by"] = "agenttrace_admission_oracle_v4,cross_policy_v4"
            current["supersession_reason"] = "V3 used an admission policy shown to be inadequate; retain only as negative diagnostic lineage."
        output.append(current)
    return output


def claim_rows(root: Path, primary_available: bool) -> list[dict]:
    agent = read_csv(root / "agenttrace_admission_oracle_v4" / "agenttrace_admission_summary.csv")
    cross = read_csv(root / "cross_policy_v4" / "cross_policy_summary.csv")
    guard = read_csv(root / "net_benefit_conflict_v4" / "net_benefit_conflict_summary.csv")
    j_summary = read_csv(root / "control_plane_v3" / "j_bound_heterogeneous_summary.csv")
    k16 = find_row(agent, mode="closed_loop", admission="coverage_first", K="16")
    k32 = find_row(agent, mode="closed_loop", admission="coverage_first", K="32")
    oracle16 = find_row(agent, mode="closed_loop", admission="oracle_future_value", K="16")
    p2c16 = find_row(cross, policy_family="p2c", interface="sketch_coverage", K="16")
    dual16 = find_row(cross, policy_family="dualmap", interface="sketch_coverage", K="16")
    guard_short = find_row(guard, coverage_tokens="256", affinity_queue_ms="50.0", policy="exact_guarded")
    affinity_short = find_row(guard, coverage_tokens="256", affinity_queue_ms="50.0", policy="affinity_first")
    guard_long = find_row(guard, coverage_tokens="4096", affinity_queue_ms="50.0", policy="exact_guarded")
    j4 = max((number(row.get("quality_loss_due_to_J_mean")) for row in j_summary if str(row.get("J")) == "4"), default=0.0)
    claims = [
        {
            "claim_id": "C1", "status": "Supported", "evidence_type": "current-schema cost microbenchmark",
            "paper_claim": "Bounded K explicitly reduces Exact-Affinity interface state cost.",
            "evidence": "Use reviewer_gap_v2/cost_scaling.csv as the primary current-schema cost table; do not describe it as end-to-end serving resource saving.",
            "safe_writing": "Report snapshot/index/update cost separately from serving latency.",
        },
        {
            "claim_id": "C2", "status": "Supported with AgentTrace-derived workload scope", "evidence_type": "trace_derived_simulation",
            "paper_claim": "Coverage-first bounded admission can preserve most of guarded Exact saved-prefill value on recorded tool-using turn structure.",
            "evidence": f"Closed-loop AgentTrace: K16 retains {number(k16['incremental_saved_vs_exact_ratio_mean']):.3f} of Exact at {number(k16['dispatcher_index_bytes_mean']):.0f}B vs Exact {number(find_row(agent, mode='closed_loop', admission='exact', K='inf')['dispatcher_index_bytes_mean']):.0f}B; K32 retains {number(k32['incremental_saved_vs_exact_ratio_mean']):.3f}. Offline Oracle-K16 reaches {number(oracle16['incremental_saved_vs_exact_ratio_mean']):.3f}.",
            "safe_writing": "Call Oracle an offline diagnostic only; describe K16 residual gap as admission headroom, not a deployable oracle result.",
        },
        {
            "claim_id": "C3", "status": "Supported as paired cross-policy replay", "evidence_type": "trace_derived_simulation",
            "paper_claim": "The bounded affinity interface adapts to both load-oriented P2C and locality-biased DualMap-style policies, with policy-dependent marginal benefit.",
            "evidence": f"P2C K16 retains {number(p2c16['incremental_saved_vs_exact_ratio_mean']):.3f} of Exact and is {number(p2c16['incremental_saved_vs_load_ratio_mean']):.3f}x Load-only; DualMap K16 retains {number(dual16['incremental_saved_vs_exact_ratio_mean']):.3f} and is {number(dual16['incremental_saved_vs_load_ratio_mean']):.3f}x DualMap-Load.",
            "safe_writing": "State interface compatibility and saved-prefill quality only; do not present these rows as live SLO/throughput evidence.",
        },
        {
            "claim_id": "C4", "status": "Supported as heterogeneous dispatcher simulation", "evidence_type": "dispatcher_level_simulation",
            "paper_claim": "Candidate fanout J is a real resource-quality control, not merely a formal bound.",
            "evidence": f"In heterogeneous J replay, maximum mean quality loss at J=4 is {j4:.4f}, while evaluated fanout obeys J.",
            "safe_writing": "Do not call J=4 production-optimal; it is scenario-specific.",
        },
        {
            "claim_id": "C5", "status": "Supported as net-benefit sensitivity simulation", "evidence_type": "dispatcher_level_simulation",
            "paper_claim": "A coverage-and-queue abstention guard prevents short/queued affinity from degrading placement.",
            "evidence": f"At 256 reusable tokens and +50ms affinity queue, affinity-first p95={number(affinity_short['ttft_p95_ms_mean']):.2f}ms; guarded p95={number(guard_short['ttft_p95_ms_mean']):.2f}ms with selection rate {number(guard_short['affinity_selected_rate_mean']):.3f}. At 4K reusable tokens and +50ms queue, guarded p95={number(guard_long['ttft_p95_ms_mean']):.2f}ms with selection rate {number(guard_long['affinity_selected_rate_mean']):.3f}.",
            "safe_writing": "Label as dispatcher-level sensitivity; it validates the decision rule, not a measured production queue.",
        },
    ]
    if primary_available:
        live = read_csv(root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_summary.csv")
        k16_live = find_row(live, locality="high", treatment_policy="sketch_coverage_k16")
        exact_live = find_row(live, locality="high", treatment_policy="exact")
        claims.append({
            "claim_id": "C6", "status": "Supported as sampled fixed-prompt live T4 evidence", "evidence_type": "live_t4_vllm",
            "paper_claim": "Under a high-locality 2K-prefix workload, bounded coverage-first routing exposes physical vLLM cached tokens and can be evaluated with input-identical paired TTFT.",
            "evidence": f"K16 paired p50 TTFT delta vs Load-only mean={number(k16_live['delta_ttft_p50_ms_mean']):.2f}ms, 95% CI=[{number(k16_live['delta_ttft_p50_ms_ci95_low']):.2f},{number(k16_live['delta_ttft_p50_ms_ci95_high']):.2f}]ms; Exact delta={number(exact_live['delta_ttft_p50_ms_mean']):.2f}ms. Inspect paired distribution before claiming a p95 improvement.",
            "safe_writing": "Use fixed prompt/output and vLLM cached-token telemetry. Describe this as sampled T4 validation, not throughput characterization.",
        })
    return claims


def write_notes(root: Path, claims: list[dict], primary_available: bool) -> None:
    lines = [
        "# B02 Paper Experiment Writing Notes (V4)", "",
        "## Read First", "",
        "Use `paper_claim_evidence_v4.csv`, `experiment_registry_v4.csv`, and `sanity_checks_v4.csv` before drafting. Evidence types are not interchangeable. `live_t4_vllm` is sampled live serving; `trace_derived_simulation` is a paired state-interface replay; `dispatcher_level_simulation` validates a decision mechanism; and cost microbenchmarks quantify interface overhead only.", "",
        "## Current Thesis", "",
        "A bounded affinity interface exposes a measurable metadata-cost versus reusable-prefill-quality trade-off. Its effectiveness depends on locality concentration, admission quality, and whether reusable coverage repays load/validation overhead. This work does not establish universal agentic workload coverage or general throughput/SLO dominance.", "",
        "## Superseded Evidence", "",
        "Do not cite `paired_t4_latency_v3`: policy namespace was embedded in semantic prompt and altered model output. Do not use `agenttrace_structural_v3` as a positive Sketch result: it is retained only as a negative admission diagnostic. V4 lineage is explicit in the master table.", "",
        "## Claim Decisions", "",
    ]
    for claim in claims:
        lines.extend([f"### {claim['claim_id']}: {claim['status']}", "", claim['paper_claim'], "", f"Evidence: {claim['evidence']}", "", f"Writing rule: {claim['safe_writing']}", ""])
    if not primary_available:
        lines.extend(["## Pending", "", "The fixed-prompt T4 primary run is not yet complete. Do not cite its smoke outputs.", ""])
    lines.extend([
        "## Required Limitations", "",
        "- AgentTrace is structural/trace-derived; it does not establish semantic task completion or production arrival realism.",
        "- Oracle-K is an upper-bound diagnosis only and must never be called deployable.",
        "- Control-plane and guard studies are simulations, not a real distributed metadata transport benchmark.",
        "- P2C/DualMap results demonstrate cross-policy interface adaptation, not universal policy gains.",
    ])
    (root / "PAPER_EXPERIMENT_RESULTS_V4.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/home/byh/B02/supplemental_20260715")
    args = parser.parse_args()
    root = Path(args.root)
    primary_available = (root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_summary.csv").is_file()
    merged = mark_lineage(read_csv(root / "AI_MASTER_TABLE_V3.csv"))
    datasets = {
        "agenttrace_admission_v4_cells": root / "agenttrace_admission_oracle_v4" / "agenttrace_admission_cells.csv",
        "agenttrace_admission_v4_summary": root / "agenttrace_admission_oracle_v4" / "agenttrace_admission_summary.csv",
        "agenttrace_admission_v4_diagnostics": root / "agenttrace_admission_oracle_v4" / "agenttrace_trace_diagnostics.csv",
        "cross_policy_v4_cells": root / "cross_policy_v4" / "cross_policy_cells.csv",
        "cross_policy_v4_summary": root / "cross_policy_v4" / "cross_policy_summary.csv",
        "net_benefit_conflict_v4_cells": root / "net_benefit_conflict_v4" / "net_benefit_conflict_cells.csv",
        "net_benefit_conflict_v4_summary": root / "net_benefit_conflict_v4" / "net_benefit_conflict_summary.csv",
    }
    if primary_available:
        datasets.update({
            "fixed_prompt_t4_v4_cells": root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_cells.csv",
            "fixed_prompt_t4_v4_pairs": root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_pairs.csv",
            "fixed_prompt_t4_v4_summary": root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_summary.csv",
        })
    loaded = {source: read_csv(path) for source, path in datasets.items()}
    for source, rows in loaded.items():
        merged.extend(source_rows(rows, source))
    write_csv(root / "AI_MASTER_TABLE_V4.csv", merged)
    registry = read_csv(root / "experiment_registry_v3.csv")
    for source, rows in loaded.items():
        if source.endswith("_cells"):
            registry.extend(registry_rows(rows, source))
    write_csv(root / "experiment_registry_v4.csv", registry)
    checks = read_csv(root / "sanity_checks_v3.csv")
    check_files = [
        root / "agenttrace_admission_oracle_v4" / "agenttrace_admission_sanity_checks.csv",
        root / "cross_policy_v4" / "cross_policy_sanity_checks.csv",
        root / "net_benefit_conflict_v4" / "net_benefit_conflict_sanity_checks.csv",
    ]
    if primary_available:
        check_files.append(root / "fixed_prompt_t4_v4_primary" / "fixed_prompt_sanity_checks.csv")
    for path in check_files:
        checks.extend(read_csv(path))
    write_csv(root / "sanity_checks_v4.csv", checks)
    claims = claim_rows(root, primary_available)
    write_csv(root / "paper_claim_evidence_v4.csv", claims)
    write_notes(root, claims, primary_available)
    exclusions = [
        {"source_dataset": "paired_t4_latency_v3", "status": "Superseded_InputConfounded", "reason": "Policy-specific namespace changed semantic prompt and output behavior; V4 fixed_prompt_t4 uses cache_salt outside prompt and fixed generation length."},
        {"source_dataset": "agenttrace_structural_v3", "status": "Legacy_DiagnosticNegative", "reason": "Legacy admission was empirically inadequate in closed loop; V4 uses coverage-first and reports Oracle-K headroom."},
        {"source_dataset": "*_smoke*", "status": "SanityOnly", "reason": "Smoke artifacts validate implementation only and are not paper evidence."},
    ]
    write_csv(root / "legacy_exclusions_v4.csv", exclusions)
    primary_sources = [
        {"paper_section": "Interface cost", "source_dataset": "reviewer_gap_v2/cost_scaling.csv", "evidence_type": "microbenchmark", "status": "Current"},
        {"paper_section": "Admission and K trade-off", "source_dataset": "agenttrace_admission_oracle_v4", "evidence_type": "trace_derived_simulation", "status": "Current"},
        {"paper_section": "Policy generality", "source_dataset": "cross_policy_v4", "evidence_type": "trace_derived_simulation", "status": "Current"},
        {"paper_section": "Fanout bound", "source_dataset": "control_plane_v3/j_bound_heterogeneous_summary.csv", "evidence_type": "dispatcher_level_simulation", "status": "Current"},
        {"paper_section": "Net-benefit guard", "source_dataset": "net_benefit_conflict_v4", "evidence_type": "dispatcher_level_simulation", "status": "Current"},
        {"paper_section": "Staleness safety", "source_dataset": "reviewer_gap_v2/staleness_validation_v2.csv,reviewer_gap_v2/toctou_races.csv", "evidence_type": "protocol simulation", "status": "Current"},
    ]
    if primary_available:
        primary_sources.append({"paper_section": "Sampled live validation", "source_dataset": "fixed_prompt_t4_v4_primary", "evidence_type": "live_t4_vllm", "status": "Current"})
    write_csv(root / "paper_primary_sources_v4.csv", primary_sources)
    (root / "AI_MASTER_TABLE_V4_README.md").write_text(
        "# AI Master Table V4\\n\\n"
        "This flat, AI-facing table preserves V2/V3 lineage and appends V4 paired replay, guard, and fixed-prompt live evidence. "
        "Filter by `status`, `source_dataset`, and `evidence_type` before comparing values. Do not cite `Superseded_InputConfounded`, `Legacy_DiagnosticNegative`, or `SanityOnly` rows as primary paper evidence.\\n"
    )
    (root / "AI_MASTER_README_V4.json").write_text(json.dumps({
        "required_reading": ["PAPER_EXPERIMENT_RESULTS_V4.md", "paper_claim_evidence_v4.csv", "paper_primary_sources_v4.csv", "experiment_registry_v4.csv", "sanity_checks_v4.csv", "legacy_exclusions_v4.csv"],
        "scope_rule": "Never pool live, trace-derived, dispatcher simulation, and microbenchmark metrics into one performance statement.",
        "primary_run_available": primary_available,
        "raw_agenttrace_text_included": False,
    }, indent=2))
    print(json.dumps({"master_rows": len(merged), "registry_rows": len(registry), "checks": len(checks), "claims": len(claims), "primary_run_available": primary_available}, indent=2))


if __name__ == "__main__":
    main()
