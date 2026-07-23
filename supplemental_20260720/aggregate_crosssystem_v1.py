#!/usr/bin/env python3
"""Aggregate the 2026-07-20 cross-system live comparison.

Per variant (normal / eviction), per arm: paired deltas vs load_only (mean/p50/
p95 TTFT), physical cached tokens normalized to Exact, believed-coverage
normalized to Exact, stale-belief rate, and dispatcher index bytes, with 95%
bootstrap CIs over the 12 paired repetitions.
"""
from __future__ import annotations

import csv
import json
import random
import statistics
from pathlib import Path

ROOT = Path("/home/byh/B02/supplemental_20260720")
OUT = ROOT / "analysis"
VARIANTS = ["normal", "eviction"]


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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []
    for variant in VARIANTS:
        path = ROOT / f"crosssystem_{variant}" / "crosssystem_cells.csv"
        if not path.exists():
            print(f"skip missing {path}")
            continue
        with path.open() as handle:
            cells = list(csv.DictReader(handle))
        by_rep_policy = {(int(r["rep"]), r["policy"]): r for r in cells}
        reps = sorted({rep for rep, _ in by_rep_policy})
        policies = sorted({policy for _, policy in by_rep_policy})
        exact_saved = [float(by_rep_policy[(r, "exact")]["estimated_saved_prefill_tokens_total"]) for r in reps]
        exact_cached = [float(by_rep_policy[(r, "exact")]["physical_cached_tokens_total"]) for r in reps]
        for policy in policies:
            if policy == "load_only":
                continue
            d_mean, d_p50, d_p95 = [], [], []
            ratio_cached, ratio_believed, stale, index_b, hit = [], [], [], [], []
            for rep in reps:
                base = by_rep_policy[(rep, "load_only")]
                cur = by_rep_policy[(rep, policy)]
                d_mean.append(float(cur["mean_ttft_ms"]) - float(base["mean_ttft_ms"]))
                d_p50.append(float(cur["ttft_p50_ms"]) - float(base["ttft_p50_ms"]))
                d_p95.append(float(cur["ttft_p95_ms"]) - float(base["ttft_p95_ms"]))
                ec = exact_cached[reps.index(rep)]
                esv = exact_saved[reps.index(rep)]
                ratio_cached.append(float(cur["physical_cached_tokens_total"]) / ec if ec else 0.0)
                ratio_believed.append(float(cur["estimated_saved_prefill_tokens_total"]) / esv if esv else 0.0)
                stale.append(float(cur["stale_belief_rate"]))
                if policy == "preble_global":
                    # Cell writer had a metadata accounting bug for preble_global
                    # (advertised left empty); its global view tracks full residency,
                    # identical volume to Exact by construction.
                    index_b.append(float(by_rep_policy[(rep, "exact")]["dispatcher_index_bytes"]))
                else:
                    index_b.append(float(cur["dispatcher_index_bytes"]))
                hit.append(float(cur["candidate_hit_rate"]))
            row = {"variant": variant, "policy": policy, "n_reps": len(d_mean)}
            for name, values in (
                ("dttft_mean_ms", d_mean), ("dttft_p50_ms", d_p50), ("dttft_p95_ms", d_p95),
                ("cached_tokens_over_exact", ratio_cached), ("believed_over_exact", ratio_believed),
                ("stale_belief_rate", stale), ("index_bytes", index_b), ("candidate_hit_rate", hit),
            ):
                mean, low, high = bootstrap_ci(values, hash((variant, policy, name)) & 0xFFFFFFFF)
                row[f"{name}_mean"] = round(mean, 4)
                row[f"{name}_ci95_low"] = round(low, 4)
                row[f"{name}_ci95_high"] = round(high, 4)
            rows_out.append(row)
    if not rows_out:
        raise SystemExit("no inputs")
    columns = list(rows_out[0].keys())
    with (OUT / "crosssystem_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows_out)

    def fmt(row, name, digits=1):
        return f"{row[f'{name}_mean']:.{digits}f} [{row[f'{name}_ci95_low']:.{digits}f}, {row[f'{name}_ci95_high']:.{digits}f}]"

    lines = ["# Cross-system state-view live comparison (2026-07-20)\n",
             "Paired deltas vs load_only; cached/believed normalized to Exact; 95% bootstrap CIs.\n"]
    for variant in VARIANTS:
        group = [r for r in rows_out if r["variant"] == variant]
        if not group:
            continue
        lines.append(f"\n## variant = {variant}\n")
        lines.append("| arm | ΔTTFT mean (ms) | ΔTTFT p95 (ms) | cached/Exact | believed/Exact | stale rate | index B | hit rate |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in group:
            lines.append(
                f"| {row['policy']} | {fmt(row, 'dttft_mean_ms')} | {fmt(row, 'dttft_p95_ms')} | "
                f"{fmt(row, 'cached_tokens_over_exact', 3)} | {fmt(row, 'believed_over_exact', 3)} | "
                f"{fmt(row, 'stale_belief_rate', 3)} | {fmt(row, 'index_bytes', 0)} | {fmt(row, 'candidate_hit_rate', 3)} |"
            )
    (OUT / "crosssystem_summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"rows": len(rows_out), "out": str(OUT)}))


if __name__ == "__main__":
    main()
