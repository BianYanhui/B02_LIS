#!/usr/bin/env python3
"""Combined analysis for the 2026-07-19 B02 supplementary live experiments.

Reads the cell tables of each supplementary run plus the original V5 primary
(alpha=0.55 anchor) and computes, per (experiment, policy):

* Rinc_est / Rinc_phys : incremental Exact-normalized value, i.e.
  (S_policy - S_load_only) / (S_exact - S_load_only) per paired repetition,
  matching the paper's Eq. 11 (R_inc).  Reported as mean + 95% bootstrap CI
  over repetitions, plus the mean Exact denominator for scale context.
* ratio_to_exact       : plain S_policy / S_exact (the V5 summary metric).
* dTTFT mean/p50/p95   : paired delta vs load_only, bootstrap CI over reps.
* index_bytes, candidate_hit_rate, abstain_rate.

Outputs:
  supplemental_20260719/analysis/combined_metrics.csv   - long table
  supplemental_20260719/analysis/combined_summary.md    - prose digest
"""
from __future__ import annotations

import csv
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path("/home/byh/B02")
OUT = ROOT / "supplemental_20260719" / "analysis"

RUNS = [
    {
        "experiment": "primary_v5_alpha0.55",
        "label": "V5 primary (alpha=0.55, conc=4)",
        "cells": ROOT / "supplemental_20260715/live_k_tradeoff_v5_primary_2k_committed/live_k_cells.csv",
        "kind": "ksweep",
        "alpha": 0.55,
        "concurrency": 4,
    },
    {
        "experiment": "s1_alpha0.05_uniform",
        "label": "S1a uniform demand (alpha=0.05, conc=4)",
        "cells": ROOT / "supplemental_20260719/live_alpha_uniform_a005/live_k_cells.csv",
        "kind": "ksweep",
        "alpha": 0.05,
        "concurrency": 4,
    },
    {
        "experiment": "s1_alpha1.35_skew",
        "label": "S1b skewed demand (alpha=1.35, conc=4)",
        "cells": ROOT / "supplemental_20260719/live_alpha_skew_a135/live_k_cells.csv",
        "kind": "ksweep",
        "alpha": 1.35,
        "concurrency": 4,
    },
    {
        "experiment": "s2_guard_ablation",
        "label": "S2 guard ablation (alpha=0.55, conc=4)",
        "cells": ROOT / "supplemental_20260719/live_guard_ablation/guard_ablation_cells.csv",
        "kind": "ksweep",
        "alpha": 0.55,
        "concurrency": 4,
    },
    {
        "experiment": "s3_concurrency8",
        "label": "S3 heavy load (alpha=0.55, conc=8)",
        "cells": ROOT / "supplemental_20260719/live_concurrency8/live_k_cells.csv",
        "kind": "ksweep",
        "alpha": 0.55,
        "concurrency": 8,
    },
]

METRIC_SAVED = "estimated_saved_prefill_tokens_total"
METRIC_CACHED = "physical_cached_tokens_total"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def bootstrap_ci(values: list[float], seed: int, resamples: int = 2000) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(means, 2.5), percentile(means, 97.5)


def load_cells(path: Path) -> list[dict]:
    with path.open() as handle:
        return list(csv.DictReader(handle))


def analyze_run(run: dict) -> list[dict]:
    cells = load_cells(run["cells"])
    by_rep_policy: dict[tuple[int, str], dict] = {}
    for row in cells:
        by_rep_policy[(int(row["rep"]), row["policy"])] = row
    reps = sorted({rep for rep, _ in by_rep_policy})
    policies = sorted({policy for _, policy in by_rep_policy})
    out_rows: list[dict] = []
    for policy in policies:
        if policy == "load_only":
            continue
        rinc_est, rinc_phys, ratio_est, ratio_phys = [], [], [], []
        d_mean, d_p50, d_p95 = [], [], []
        exact_denoms, load_saved = [], []
        index_bytes, hit_rate, abstain_rate = [], [], []
        for rep in reps:
            base = by_rep_policy.get((rep, "load_only"))
            treat = by_rep_policy.get((rep, policy))
            exact = by_rep_policy.get((rep, "exact"))
            if not base or not treat or not exact:
                continue
            s_load = float(base[METRIC_SAVED])
            s_treat = float(treat[METRIC_SAVED])
            s_exact = float(exact[METRIC_SAVED])
            c_load = float(base[METRIC_CACHED])
            c_treat = float(treat[METRIC_CACHED])
            c_exact = float(exact[METRIC_CACHED])
            exact_denoms.append(s_exact)
            load_saved.append(s_load)
            denom = s_exact - s_load
            rinc_est.append((s_treat - s_load) / denom if abs(denom) > 1e-9 else float("nan"))
            cdenom = c_exact - c_load
            rinc_phys.append((c_treat - c_load) / cdenom if abs(cdenom) > 1e-9 else float("nan"))
            ratio_est.append(s_treat / s_exact if s_exact else float("nan"))
            ratio_phys.append(c_treat / c_exact if c_exact else float("nan"))
            d_mean.append(float(treat["mean_ttft_ms"]) - float(base["mean_ttft_ms"]))
            d_p50.append(float(treat["ttft_p50_ms"]) - float(base["ttft_p50_ms"]))
            d_p95.append(float(treat["ttft_p95_ms"]) - float(base["ttft_p95_ms"]))
            index_bytes.append(float(treat["dispatcher_index_bytes"]))
            hit_rate.append(float(treat["candidate_hit_rate"]))
            abstain_rate.append(float(treat.get("abstain_rate", 0.0) or 0.0))
        seed = hash((run["experiment"], policy)) & 0xFFFFFFFF
        row = {
            "experiment": run["experiment"],
            "label": run["label"],
            "alpha": run["alpha"],
            "concurrency": run["concurrency"],
            "policy": policy,
            "n_reps": len(rinc_est),
            "mean_exact_saved_prefill_denom": statistics.mean(exact_denoms) if exact_denoms else 0.0,
            "mean_load_saved_prefill": statistics.mean(load_saved) if load_saved else 0.0,
        }
        for name, values in (
            ("rinc_est", rinc_est), ("rinc_phys", rinc_phys),
            ("ratio_est", ratio_est), ("ratio_phys", ratio_phys),
            ("dttft_mean_ms", d_mean), ("dttft_p50_ms", d_p50), ("dttft_p95_ms", d_p95),
            ("index_bytes", index_bytes), ("candidate_hit_rate", hit_rate),
            ("abstain_rate", abstain_rate),
        ):
            clean = [v for v in values if v == v]  # drop NaN
            mean, low, high = bootstrap_ci(clean, seed + hash(name) % 1000)
            row[f"{name}_mean"] = round(mean, 4)
            row[f"{name}_ci95_low"] = round(low, 4)
            row[f"{name}_ci95_high"] = round(high, 4)
        out_rows.append(row)
    return out_rows


def fmt(row: dict, name: str, scale: float = 1.0, digits: int = 3) -> str:
    return f"{row[f'{name}_mean']*scale:.{digits}f} [{row[f'{name}_ci95_low']*scale:.{digits}f}, {row[f'{name}_ci95_high']*scale:.{digits}f}]"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    missing: list[str] = []
    for run in RUNS:
        if not run["cells"].exists():
            missing.append(str(run["cells"]))
            continue
        all_rows.extend(analyze_run(run))
    if missing:
        print("WARNING missing inputs:\n  " + "\n  ".join(missing), file=sys.stderr)
    if not all_rows:
        raise SystemExit("no inputs found")
    columns = list(dict.fromkeys(key for row in all_rows for key in row))
    with (OUT / "combined_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(all_rows)

    lines: list[str] = []
    lines.append("# Combined supplementary live-experiment summary (2026-07-19)\n")
    lines.append("Rinc = (S_policy - S_load_only) / (S_exact - S_load_only) per paired rep; "
                 "dTTFT = paired delta vs load_only. 95% bootstrap CIs over repetitions.\n")
    current = None
    for row in all_rows:
        if row["experiment"] != current:
            current = row["experiment"]
            lines.append(f"\n## {row['label']}\n")
            lines.append("| policy | Rinc_est | Rinc_phys | dTTFT mean (ms) | dTTFT p50 (ms) | dTTFT p95 (ms) | index B | hit rate | abstain |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
        lines.append(
            f"| {row['policy']} | {fmt(row, 'rinc_est')} | {fmt(row, 'rinc_phys')} | "
            f"{fmt(row, 'dttft_mean_ms', digits=1)} | {fmt(row, 'dttft_p50_ms', digits=1)} | "
            f"{fmt(row, 'dttft_p95_ms', digits=1)} | {fmt(row, 'index_bytes', digits=0)} | "
            f"{fmt(row, 'candidate_hit_rate', digits=3)} | {fmt(row, 'abstain_rate', digits=3)} |"
        )
    (OUT / "combined_summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"rows": len(all_rows), "out": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
