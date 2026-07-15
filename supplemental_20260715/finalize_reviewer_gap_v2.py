#!/usr/bin/env python3
"""Normalize provenance and create paper-facing tables for B02 reviewer-gap v2."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean


LEGACY = [
    ("supp_Table_A_clean", "Legacy_DoNotCite", "Sketch=2B is an old semantic-sketch schema", "cost_scaling.csv"),
    ("supp_Table_E_ablation", "Legacy_DoNotCite", "schema drift from current affinity entry", "cost_scaling.csv"),
    ("supp_Table_G_claims", "Legacy_DoNotCite", "contains an invalid Sketch-beats-Oracle claim", "paper_claim_evidence.csv"),
    ("full_final_claims", "Legacy_DoNotCite", "old claim set", "paper_claim_evidence.csv"),
    ("full_tradeoff", "Legacy_DoNotCite", "old state-interface schema", "cost_scaling.csv"),
    ("full_dispatch_quality", "Legacy_DoNotCite", "policy and state-view labels are mixed", "trace_replay_quality.csv"),
    ("exp_part_a_real_serving", "Legacy_DoNotCite", "r1/r2 are not independent repetitions", "trace_replay_quality.csv"),
    ("eventdriven_all_results", "Legacy_DoNotCite", "old simulator lacks token-bucket chain", "budget_freshness_quality.csv"),
    ("ksweep_all_summaries", "Legacy_TTFT_only", "same_inst_step_ratio is non-discriminative", "trace_replay_quality.csv"),
    ("n256_*", "Legacy_Mock", "mock dispatcher, not 256-GPU serving", "j_bound_sweep.csv"),
    ("trace_replay_v2_sanity_prepolicyfix", "Sanity_only", "adapter did not hold native policy fixed", "trace_replay_v3/trace_replay_quality.csv"),
    ("trace_replay_v2_sanity_unisolated", "Sanity_only", "closed-loop policies shared physical vLLM cache namespaces", "trace_replay_v3/trace_replay_quality.csv"),
]


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    columns, seen = [], set()
    for row in rows:
        for column in row:
            if column not in seen:
                columns.append(column)
                seen.add(column)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def replace_commit(path: Path, commit: str) -> list[dict]:
    rows = read_csv(path)
    for row in rows:
        row["code_commit"] = commit
    write_csv(path, rows)
    return rows


def record_registry(rows: list[dict], source: str, supersedes: str) -> list[dict]:
    registry = []
    for index, row in enumerate(rows):
        registry.append({
            "experiment_id": row.get("experiment_id", f"20260715_{source}_{index:05d}"),
            "evidence_type": row.get("evidence_type", "simulation"),
            "code_commit": row.get("code_commit", ""),
            "model": row.get("model", ""),
            "hardware": row.get("hardware", ""),
            "N": row.get("N", row.get("n_instances", "")),
            "K": row.get("K", ""),
            "J": row.get("J", ""),
            "rate_budget_Bps": row.get("rate_budget_Bps", ""),
            "workload_trace_hash": row.get("workload_trace_hash", ""),
            "seed": row.get("seed", ""),
            "repetitions": row.get("repetitions", 1),
            "prefix_length_distribution": "{256,512}" if source == "trace_replay_quality" else "{256,512,1024,2048}",
            "locality": row.get("locality", "n/a"),
            "cache_capacity": row.get("cache_capacity", row.get("R_per_inst", "n/a")),
            "eviction_policy": "fixed resident snapshot / bounded LRU simulator" if row.get("evidence_type") != "live_t4_vllm" else "vLLM prefix cache plus dispatcher-observed LRU mirror",
            "status": row.get("status", "Current"),
            "source_sheet": source,
            "supersedes": supersedes,
            "supersession_reason": "2026-07-15 reviewer-gap v2 schema, traces, and provenance",
        })
    return registry


def data_dictionary() -> list[dict]:
    return [
        {"field": "candidate_hit_rate", "meaning": "Requests whose raw affinity candidate set contains at least one candidate.", "unit": "fraction", "notes": "Does not count candidate<=1 as a special case; it is a raw opportunity metric."},
        {"field": "observed_reuse_hit_rate", "meaning": "Dispatcher-observed reuse: selected instance had previously received the same prefix in the cell.", "unit": "fraction", "notes": "Not a private vLLM cache counter."},
        {"field": "metadata_snapshot_bytes", "meaning": "Serialized interface-size model including coarse base N*96B and affinity entries.", "unit": "bytes", "notes": "Sketch entries are per instance, bounded by K."},
        {"field": "K", "meaning": "Per-instance advertised top-K affinity entries.", "unit": "entries/instance", "notes": "Never global K."},
        {"field": "raw_candidate_fanout_p95", "meaning": "P95 candidate count before J truncation.", "unit": "instances", "notes": "May exceed J."},
        {"field": "evaluated_candidate_fanout_p95", "meaning": "P95 candidate count actually evaluated after J truncation.", "unit": "instances", "notes": "Must be <= J."},
        {"field": "saved_vs_exact_ratio", "meaning": "Bounded-admission saved Prefill work divided by full Exact Affinity saved work.", "unit": "ratio", "notes": "Defined only when exact_saved_ms_total>0."},
        {"field": "stale_lookup_miss_rate", "meaning": "Candidate set nonempty but no candidate remains valid at lookup time, divided by all requests.", "unit": "fraction", "notes": "Separate from normal_cache_miss."},
        {"field": "unsafe_reuse_rate", "meaning": "Fraction of requests that reuse incompatible or absent state.", "unit": "fraction", "notes": "Must be zero with validation enabled."},
        {"field": "state_size vs snapshot_B", "meaning": "state_size is in-memory object size; snapshot_B is serialized control-plane bytes.", "unit": "bytes", "notes": "Do not interchange the two."},
        {"field": "locality", "meaning": "Synthetic Zipf prefix popularity. CPU: alpha in {1.25,0.85,0.15}; live: alpha in {1.35,0.75,0.05}.", "unit": "category", "notes": "The two alpha sets must not be pooled."},
    ]


def conclusion_rows(cpu: Path, trace: Path, sota: Path) -> list[dict]:
    cost = read_csv(cpu / "cost_scaling.csv")
    target_cost = [row for row in cost if row["N"] == "128" and row["R_per_inst"] == "256" and row["K"] == "8"]
    cost_by = {row["interface"]: row for row in target_cost}
    admission = read_csv(cpu / "supp_admission_v2.csv")
    high = [as_float(row["saved_vs_exact_ratio"]) for row in admission if row["locality"] == "high" and row["K"] == "8" and row["variant"] == "demand_aware_full"]
    medium = [as_float(row["saved_vs_exact_ratio"]) for row in admission if row["locality"] == "medium" and row["K"] == "16" and row["variant"] == "demand_aware_full"]
    shifts = [as_float(row["convergence_delay"]) for row in admission if row["locality"] == "high_to_low"]
    stale = read_csv(cpu / "staleness_validation_v2.csv")
    races = read_csv(cpu / "toctou_races.csv")
    budget = read_csv(cpu / "budget_freshness_quality.csv")
    live = read_csv(trace / "trace_replay_quality.csv")
    frozen_exact = [row for row in live if row["mode"] == "frozen" and row["policy"] == "exact" and row["locality"] == "high"][0]
    frozen_sk8 = [row for row in live if row["mode"] == "frozen" and row["policy"] == "sketch_k8" and row["locality"] == "high"][0]
    closed_exact = [row for row in live if row["mode"] == "closed_loop" and row["policy"] == "exact" and row["locality"] == "high"][0]
    closed_sk16 = [row for row in live if row["mode"] == "closed_loop" and row["policy"] == "sketch_k16" and row["locality"] == "high"][0]
    b4low = [row for row in budget if row["baseline"] == "event_driven_token_bucket" and row["churn_events_per_inst_s"] == "0.01" and row["rate_budget_Bps"] == "256"][0]
    b1low = [row for row in budget if row["baseline"] == "periodic_full" and row["churn_events_per_inst_s"] == "0.01" and row["rate_budget_Bps"] == "256"][0]
    b4mid = [row for row in budget if row["baseline"] == "event_driven_token_bucket" and row["churn_events_per_inst_s"] == "1.0" and row["rate_budget_Bps"] == "256"][0]
    b3high = [row for row in budget if row["baseline"] == "event_driven_no_rate" and row["churn_events_per_inst_s"] == "5.0" and row["rate_budget_Bps"] == "256"][0]
    return [
        {"claim_id": "C1", "status": "Supported", "paper_claim": "Bounded Sketch reduces the current Exact Affinity interface cost.", "evidence": f"N=128,R=256,K=8: snapshot Sketch={cost_by['sketch']['snapshot_B']}B vs Exact={cost_by['exact_affinity']['snapshot_B']}B; update Sketch={cost_by['sketch']['event_driven_Bps']}B/s vs Exact={cost_by['exact_affinity']['event_driven_Bps']}B/s.", "safe_writing": "Use as the replacement for legacy Rich-size Table 1."},
        {"claim_id": "C2", "status": "Supported with workload scope", "paper_claim": "Exact visibility is an information upper bound and bounded K can approach it under locality.", "evidence": f"Frozen high locality: Exact reuse={frozen_exact['observed_reuse_hit_rate_mean']}; Sketch-K8={frozen_sk8['observed_reuse_hit_rate_mean']}. Closed-loop high: Exact={closed_exact['observed_reuse_hit_rate_mean']}; Sketch-K16={closed_sk16['observed_reuse_hit_rate_mean']}.", "safe_writing": "State frozen results as controlled interface evidence; label closed-loop observed reuse as dispatcher-observed."},
        {"claim_id": "C3", "status": "Supported", "paper_claim": "Validation keeps stale metadata from authorizing unsafe reuse.", "evidence": f"Validation-on max unsafe reuse={max(as_float(row['unsafe_reuse_rate']) for row in stale if row['validation_mode']=='on')}; seven TOCTOU race maximum unsafe count={max(int(row['unsafe_reuse_count']) for row in races)}.", "safe_writing": "Correctness evidence only, not a latency speedup."},
        {"claim_id": "C4", "status": "Supported as simulation", "paper_claim": "Budgeted event-driven dissemination can retain quality under low/mid churn and exposes no-rate overload.", "evidence": f"B4 low churn saved={b4low['saved_prefill_ms_total']} vs periodic={b1low['saved_prefill_ms_total']}; B4 mid stale={b4mid['stale_lookup_rate']}; B3 high-churn p95 delay={b3high['update_delay_p95_ms']}ms.", "safe_writing": "Call this a control-plane simulation, not a live metadata transport benchmark."},
        {"claim_id": "C5", "status": "Negative result / Route B", "paper_claim": "Current demand-aware utility should not be presented as the default superior admission policy.", "evidence": f"Full utility mean saved/exact: high K8={mean(high):.3f}, medium K16={mean(medium):.3f}; high-to-low convergence mean={mean(shifts):.0f} requests.", "safe_writing": "Describe demand-aware admission as pluggable. Use LRU/coverage as default bounded admission in this workload generator."},
    ]


def write_paper_notes(path: Path, claims: list[dict]) -> None:
    lines = [
        "# B02 Paper Experiment Writing Notes (v2)",
        "",
        "Use only `Current` rows in `experiment_registry_v2.csv` as primary evidence. Do not cite sources listed as `Legacy_DoNotCite` or `Sanity_only` in `legacy_data_status.csv`.",
        "",
        "## Recommended Evaluation Structure",
        "",
        "1. **Current-schema interface cost.** Report `cost_scaling.csv` at N=128, R=256, K=8. It isolates Load-only, Exact Affinity, and Sketch under the current 64-byte affinity-entry schema.",
        "2. **Same-trace state-interface quality.** Use frozen rows for the information-bound result and closed-loop rows for sampled live T4 TTFT/reuse validation. State exactly that observed reuse is dispatcher-observed, because vLLM does not export cache-hit counters.",
        "3. **Cross-policy generality.** Use the existing `sota_policy_matrix` CPU traces for DualMap-style, Power-of-Two, and SLO-aware families. Keep their modeled TTFT separate from live TTFT.",
        "4. **Bounds and correctness.** Use `j_bound_sweep.csv`, `budget_freshness_quality.csv`, `staleness_validation_v2.csv`, and `toctou_races.csv` as explicit control-plane simulation evidence.",
        "",
        "## Claim Decisions",
        "",
    ]
    for claim in claims:
        lines.extend([f"### {claim['claim_id']}: {claim['status']}", "", claim["paper_claim"], "", f"Evidence: {claim['evidence']}", "", f"Writing rule: {claim['safe_writing']}", ""])
    lines.extend([
        "## T4 Scope",
        "",
        "Four Tesla T4 GPUs run one Qwen2.5-1.5B vLLM instance per GPU. The measured trace replay uses 64 requests per cell, 5 independent repetitions, approximate 256/512-token prompts, and four-request arrival waves. This is a sampled end-to-end validation, not a 1,050,000-request throughput study. A larger GPU (for example A800/80G) is needed for long-context or high-concurrency throughput claims.",
        "",
        "## Admission Negative Result",
        "",
        "The current decentralized demand-aware utility did not meet its preregistered saved-vs-Exact or shift-convergence targets. Revise the paper so Sketch is a bounded state interface with pluggable admission; use LRU/coverage admission as the default bounded baseline and report demand-aware as an ablation/limitation rather than a superior optimizer.",
    ])
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/home/byh/B02/supplemental_20260715")
    parser.add_argument("--cpu-commit", required=True)
    parser.add_argument("--trace-commit", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    cpu, trace, sota = root / "reviewer_gap_v2", root / "trace_replay_v3", root / "sota_policy_matrix"
    cpu_files = ["cost_scaling.csv", "supp_admission_v2.csv", "j_bound_sweep.csv", "staleness_validation_v2.csv", "toctou_races.csv", "budget_freshness_quality.csv"]
    all_cpu = {name: replace_commit(cpu / name, args.cpu_commit) for name in cpu_files}
    trace_cells = replace_commit(trace / "trace_replay_quality_cells.csv", args.trace_commit)
    trace_summary = read_csv(trace / "trace_replay_quality.csv")
    registry = []
    for name, rows in all_cpu.items():
        registry.extend(record_registry(rows, name.removesuffix(".csv"), "legacy control-plane tables"))
    registry.extend(record_registry(trace_cells, "trace_replay_quality", "live_vllm_affinity_workload"))
    write_csv(root / "experiment_registry_v2.csv", registry)
    write_csv(root / "legacy_data_status.csv", [{"source_sheet": s, "status": st, "reason": r, "superseded_by": by} for s, st, r, by in LEGACY])
    write_csv(root / "data_dictionary_v2.csv", data_dictionary())
    claims = conclusion_rows(cpu, trace, sota)
    write_csv(root / "paper_claim_evidence.csv", claims)
    write_paper_notes(root / "PAPER_EXPERIMENT_RESULTS_V2.md", claims)
    cpu_checks = read_csv(cpu / "sanity_checks.csv")
    trace_checks = read_csv(trace / "trace_replay_sanity_checks.csv")
    for row in cpu_checks:
        if row["check_name"] == "frozen Exact contains advertised Sketch":
            row["status"] = "PASS" if all(check["status"] == "PASS" for check in trace_checks if "frozen Exact upper bound" in check["check_name"]) else "FAIL"
            row["suggested_fix"] = "fixed-snapshot live replay verifies Exact information upper bound"
    cpu_checks.extend(trace_checks)
    write_csv(root / "sanity_checks_v2.csv", cpu_checks)
    readme = {
        "purpose": "Current, AI-facing experimental evidence for Cost-Aware State Interfaces for LLM Request Dispatch.",
        "primary_current_sources": ["reviewer_gap_v2/*.csv", "trace_replay_v2/trace_replay_quality.csv", "sota_policy_matrix/*.csv"],
        "legacy_sources": "legacy_data_status.csv; do not cite Legacy_DoNotCite or Sanity_only rows.",
        "key_negative_result": "Current decentralized demand-aware admission does not meet its preregistered quality/convergence thresholds; use Route B wording.",
        "live_limit": "4-T4 replay uses 64 requests/cell, 5 repetitions, 256/512 approximate-token prompts, and four-request arrival waves. It is sampled live validation, not a million-request throughput benchmark.",
    }
    (root / "AI_MASTER_README_v2.json").write_text(json.dumps(readme, indent=2))
    print(json.dumps({"registry_rows": len(registry), "claims": len(claims), "checks": len(cpu_checks)}, indent=2))


if __name__ == "__main__":
    main()
