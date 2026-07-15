#!/usr/bin/env python3
"""Merge B02 v2 evidence and reviewer-driven v3 supplements into AI tables."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def number(value: object) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def registry_rows(rows: list[dict], source: str, commit: str) -> list[dict]:
    output = []
    for index, row in enumerate(rows):
        output.append({
            "experiment_id": row.get("experiment_id", f"20260715_v3_{source}_{index:05d}"),
            "evidence_type": row.get("evidence_type", "simulation"),
            "code_commit": commit,
            "model": row.get("model", ""),
            "hardware": row.get("hardware", ""),
            "N": row.get("N", row.get("n_instances", "")),
            "K": row.get("K", ""),
            "J": row.get("J", ""),
            "rate_budget_Bps": row.get("rate_budget_Bps", ""),
            "workload_trace_hash": row.get("workload_trace_hash", row.get("source_file_sha256", "")),
            "seed": row.get("seed", ""),
            "repetitions": row.get("repetitions", 1),
            "prefix_length_distribution": row.get("prefix_length_distribution", "trace-derived or experiment-specific"),
            "locality": row.get("locality", "n/a"),
            "cache_capacity": row.get("cache_capacity", "n/a"),
            "eviction_policy": row.get("eviction_policy", "simulation-specific bounded LRU"),
            "status": row.get("status", "Current"),
            "source_sheet": source,
            "supersedes": "reviewer feedback 2026-07-15",
            "supersession_reason": "adds paired T4 statistics, heterogeneous J candidates, repeated control-plane runs, or public trace-derived structure",
        })
    return output


def claim_rows(root: Path) -> list[dict]:
    claims = read_csv(root / "paper_claim_evidence.csv")
    j_summary = read_csv(root / "control_plane_v3" / "j_bound_heterogeneous_summary.csv")
    budget = read_csv(root / "control_plane_v3" / "budget_freshness_summary_v3.csv")
    agent = read_csv(root / "agenttrace_structural_v3" / "agenttrace_structural_summary.csv")
    paired = read_csv(root / "paired_t4_latency_v3" / "paired_latency_summary.csv")
    j_at_4 = [number(row["quality_loss_due_to_J_mean"]) for row in j_summary if row["J"] == "4"]
    no_rate = [number(row["update_delay_p95_ms_mean"]) for row in budget if row["baseline"] == "event_driven_no_rate" and row["churn_events_per_inst_s"] == "5.0"]
    agent_exact = next(row for row in agent if row["mode"] == "closed_loop" and row["policy"] == "exact")
    agent_sk8 = next(row for row in agent if row["mode"] == "closed_loop" and row["policy"] == "sketch_k8")
    high_pairs = [row for row in paired if row["locality"] == "high" and row["treatment_policy"] == "sketch_k8"]
    high_evidence = "paired run unavailable"
    if high_pairs:
        row = high_pairs[0]
        high_evidence = (
            f"Sketch-K8 vs load-only paired p95 delta median={number(row['delta_ttft_p95_ms_median']):.2f}ms, "
            f"95% bootstrap CI for mean=[{number(row['delta_ttft_p95_ms_ci95_low']):.2f}, {number(row['delta_ttft_p95_ms_ci95_high']):.2f}]ms; "
            f"negative-delta fraction={number(row['delta_ttft_p95_ms_fraction_below_zero']):.2f}."
        )
    claims.extend([
        {
            "claim_id": "C6",
            "status": "Supported as heterogeneous control-plane simulation",
            "paper_claim": "J is a real resource-quality knob when affinity candidates differ in coverage, queue delay, and expiry risk.",
            "evidence": f"Across heterogeneous candidate cells, maximum mean J=4 quality loss is {max(j_at_4):.4f}; all evaluated p95 fanouts obey J.",
            "safe_writing": "Present a J Pareto curve and label it a dispatcher-level simulation; do not claim a production-optimal J.",
        },
        {
            "claim_id": "C7",
            "status": "Supported with trace-derived structural scope",
            "paper_claim": "The interface trade-off also appears under recorded tool-using agent turn structure.",
            "evidence": f"Public AgentTrace closed-loop structural replay: Exact saved={number(agent_exact['saved_prefill_tokens_total_mean']):.1f} prefix tokens; Sketch-K8={number(agent_sk8['saved_prefill_tokens_total_mean']):.1f}.",
            "safe_writing": "Call this trace-derived structural replay. It contains no raw text and does not establish semantic task accuracy or live latency.",
        },
        {
            "claim_id": "C8",
            "status": "Sampled live evidence only",
            "paper_claim": "Order-balanced T4 replay characterizes the paired latency distribution without an outlier-dominated aggregate.",
            "evidence": high_evidence,
            "safe_writing": "Report paired median/IQR/scatter and CI. Claim a TTFT improvement only when the paired distribution, not an unpaired mean, supports it.",
        },
        {
            "claim_id": "C9",
            "status": "Supported as repeated simulation",
            "paper_claim": "Control-plane freshness conclusions are reported with independent repetitions and confidence intervals.",
            "evidence": f"Repeated finite-link sensitivity includes 20 repetitions/configuration; event-driven no-rate high-churn p95 delay reaches up to {max(no_rate):.1f}ms.",
            "safe_writing": "Retain the control-plane simulation label and avoid describing it as a measured metadata transport deployment.",
        },
    ])
    return claims


def paper_notes(root: Path, claims: list[dict]) -> None:
    lines = [
        "# B02 Paper Experiment Writing Notes (v3)",
        "",
        "Use rows with `status=Current` in `experiment_registry_v3.csv`. Evidence types are not interchangeable: `live_t4_vllm` is sampled live serving, `trace_replay_simulation` and `trace_derived_simulation` are controlled state-interface evidence, and `control_plane_simulation` is not a live network benchmark.",
        "",
        "## Evidence hierarchy",
        "",
        "1. Interface cost: current-schema cost scaling is the primary quantitative cost claim.",
        "2. Quality: frozen replay establishes the Exact information upper bound; large frozen traces establish locality scope.",
        "3. Live T4: use only paired distributions from `paired_t4_latency_v3`, not an unpaired p95 mean dominated by a single repetition.",
        "4. Agent workload: `agenttrace_structural_v3` derives lengths, turn structure and hashed lineage from public tool-execution traces; it is not a semantic task or live serving benchmark.",
        "5. Control plane: heterogenous J and repeated budget sensitivity are explicitly simulations.",
        "",
        "## Mandatory scope language",
        "",
        "The paper should claim a bounded state-interface cost-quality trade-off for prefix-reusing workloads. It should not claim universal agentic workload coverage, a general throughput improvement, or a universally superior demand-aware admission policy.",
        "",
        "## Claim decisions",
        "",
    ]
    for claim in claims:
        lines.extend([f"### {claim['claim_id']}: {claim['status']}", "", claim["paper_claim"], "", f"Evidence: {claim['evidence']}", "", f"Writing rule: {claim['safe_writing']}", ""])
    (root / "PAPER_EXPERIMENT_RESULTS_V3.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/home/byh/B02/supplemental_20260715")
    parser.add_argument("--v3-commit", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    v2_master = read_csv(root / "AI_MASTER_TABLE_V2.csv")
    v2_registry = read_csv(root / "experiment_registry_v2.csv")
    datasets = {
        "control_plane_v3_j_replicates": read_csv(root / "control_plane_v3" / "j_bound_heterogeneous_replicates.csv"),
        "control_plane_v3_j_summary": read_csv(root / "control_plane_v3" / "j_bound_heterogeneous_summary.csv"),
        "control_plane_v3_budget_replicates": read_csv(root / "control_plane_v3" / "budget_freshness_replicates_v3.csv"),
        "control_plane_v3_budget_summary": read_csv(root / "control_plane_v3" / "budget_freshness_summary_v3.csv"),
        "agenttrace_structural_cells": read_csv(root / "agenttrace_structural_v3" / "agenttrace_structural_cells.csv"),
        "agenttrace_structural_summary": read_csv(root / "agenttrace_structural_v3" / "agenttrace_structural_summary.csv"),
        "paired_t4_latency_cells": read_csv(root / "paired_t4_latency_v3" / "paired_latency_cells.csv"),
        "paired_t4_latency_pairs": read_csv(root / "paired_t4_latency_v3" / "paired_latency_pairs.csv"),
        "paired_t4_latency_summary": read_csv(root / "paired_t4_latency_v3" / "paired_latency_summary.csv"),
    }
    merged = list(v2_master)
    for source, rows in datasets.items():
        for row in rows:
            normalized = {"source_dataset": source, "evidence_type": row.get("evidence_type", "simulation"), "status": row.get("status", "Current"), "code_commit": args.v3_commit, **row}
            normalized["code_commit"] = args.v3_commit
            merged.append(normalized)
    write_csv(root / "AI_MASTER_TABLE_V3.csv", merged)
    registry = list(v2_registry)
    for source in ("control_plane_v3_j_replicates", "control_plane_v3_budget_replicates", "agenttrace_structural_cells", "paired_t4_latency_cells"):
        registry.extend(registry_rows(datasets[source], source, args.v3_commit))
    write_csv(root / "experiment_registry_v3.csv", registry)
    v2_checks = read_csv(root / "sanity_checks_v2.csv")
    new_checks = []
    for name in ("control_plane_v3/control_plane_v3_sanity_checks.csv", "agenttrace_structural_v3/agenttrace_structural_sanity_checks.csv", "paired_t4_latency_v3/paired_latency_sanity_checks.csv"):
        new_checks.extend(read_csv(root / name))
    write_csv(root / "sanity_checks_v3.csv", v2_checks + new_checks)
    claims = claim_rows(root)
    write_csv(root / "paper_claim_evidence_v3.csv", claims)
    paper_notes(root, claims)
    readme = {
        "purpose": "Merged AI-facing B02 evidence after reviewer-driven v3 supplements.",
        "required_reading": ["PAPER_EXPERIMENT_RESULTS_V3.md", "paper_claim_evidence_v3.csv", "experiment_registry_v3.csv", "sanity_checks_v3.csv"],
        "scope_rule": "Do not pool live, trace-derived, and control-plane simulation metrics as one latency claim.",
        "agenttrace_privacy_rule": "Only derived length/hash/turn structure appears in output; raw agent source text is excluded from the AI master table.",
    }
    (root / "AI_MASTER_README_v3.json").write_text(json.dumps(readme, indent=2))
    (root / "AI_MASTER_TABLE_V3_README.md").write_text(
        "# AI Master Table v3\n\n"
        "This flat table contains all V2 evidence plus V3 reviewer-driven supplements. "
        "Use `source_dataset`, `evidence_type`, and `metric_scope` before comparing rows. "
        "The source AgentTrace raw JSONL is intentionally excluded.\n"
    )
    print(json.dumps({"master_rows": len(merged), "registry_rows": len(registry), "checks": len(v2_checks) + len(new_checks), "claims": len(claims)}, indent=2))


if __name__ == "__main__":
    main()
