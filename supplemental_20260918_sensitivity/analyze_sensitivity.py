#!/usr/bin/env python3
"""Build the two-panel sensitivity figure and the CSV tables cited in the text."""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path("/home/byh/B02/supplemental_20260918_sensitivity")
C0S = [256, 512, 1024, 2048]
THETAS = [10, 20, 30, 60]
LENGTHS = [1024, 2048, 4096, 8192]


def read_csv(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def fnum(row: dict, key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    if value in (None, ""):
        return default
    return float(value)


def mean_ci(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return mean, 1.96 * math.sqrt(var / len(values))


def load_cells() -> list[dict]:
    rows: list[dict] = []
    summary = ROOT / "summary"
    if not summary.is_dir():
        return rows
    for path in sorted(summary.glob("cells_panel_*.csv")):
        rows.extend(read_csv(path))
    return rows


def panel_a_table(cells: list[dict]) -> list[dict]:
    static = defaultdict(list)
    adaptive = defaultdict(list)
    for row in cells:
        if row.get("workload") != "reuse_intensive":
            continue
        prefix = int(fnum(row, "prefix_length"))
        if row.get("policy") == "StaticSemantic" and prefix == 0:
            static[int(row["rep"])].append(fnum(row, "ttft_mean_ms"))
        if row.get("policy") == "Adaptive" and prefix == 0:
            c0 = int(round(fnum(row, "c0_tokens")))
            theta = int(round(fnum(row, "theta_s")))
            adaptive[(c0, theta, int(row["rep"]))].append(fnum(row, "ttft_mean_ms"))
    out = []
    for c0 in C0S:
        for theta in THETAS:
            deltas = []
            for rep, static_vals in static.items():
                if not static_vals:
                    continue
                adapt_vals = adaptive.get((c0, theta, rep), [])
                if not adapt_vals:
                    continue
                deltas.append(sum(static_vals) / len(static_vals) - sum(adapt_vals) / len(adapt_vals))
            mean, ci = mean_ci(deltas)
            out.append({
                "c0_tokens": c0,
                "theta_s": theta,
                "n_reps": len(deltas),
                "delta_ttft_ms_mean": mean,
                "delta_ttft_ms_ci95": ci,
                "ci_includes_zero": bool(len(deltas) >= 2 and abs(mean) <= ci),
                "paper_point": int(c0 == 1024 and theta == 30),
            })
    return out


def panel_b_table(cells: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    missing = defaultdict(list)
    forwarded = defaultdict(list)
    for row in cells:
        if row.get("policy") not in ("RateFIFO", "StaticSemantic", "Adaptive"):
            continue
        length = int(fnum(row, "prefix_length"))
        if length <= 0:
            continue
        key = (length, row["policy"])
        grouped[key].append(fnum(row, "ttft_mean_ms"))
        missing[key].append(fnum(row, "dispatcher_view_missing_at_dispatch_rate"))
        forwarded[key].append(fnum(row, "relay_forwarded", default=fnum(row, "net_msgs_delivered")))
    out = []
    for length in LENGTHS:
        for policy in ("RateFIFO", "StaticSemantic", "Adaptive"):
            ttfts = grouped.get((length, policy), [])
            mean, ci = mean_ci(ttfts)
            miss_mean, miss_ci = mean_ci(missing.get((length, policy), []))
            fwd_mean, fwd_ci = mean_ci(forwarded.get((length, policy), []))
            out.append({
                "prefix_length": length,
                "policy": policy,
                "n_reps": len(ttfts),
                "ttft_mean_ms": mean,
                "ttft_ci95_ms": ci,
                "view_missing_mean": miss_mean,
                "view_missing_ci95": miss_ci,
                "relay_forwarded_mean": fwd_mean,
                "relay_forwarded_ci95": fwd_ci,
            })
    return out


def draw_figure(panel_a: list[dict], panel_b: list[dict], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.15), constrained_layout=True)
    grid = np.full((len(THETAS), len(C0S)), np.nan)
    hatch = np.zeros_like(grid, dtype=bool)
    by_key = {(int(row["c0_tokens"]), int(row["theta_s"])): row for row in panel_a}
    for i, theta in enumerate(THETAS):
        for j, c0 in enumerate(C0S):
            row = by_key.get((c0, theta))
            if not row or int(row["n_reps"]) == 0:
                continue
            grid[i, j] = float(row["delta_ttft_ms_mean"])
            hatch[i, j] = bool(int(float(row["ci_includes_zero"])))
    ax = axes[0]
    finite = np.isfinite(grid)
    vmax = max(80.0, float(np.nanmax(np.abs(grid))) if finite.any() else 80.0)
    image = ax.imshow(grid, origin="upper", cmap="RdBu", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(C0S)), [str(v) for v in C0S])
    ax.set_yticks(range(len(THETAS)), [str(v) for v in THETAS])
    ax.set_xlabel(r"$C_0=\lambda b$ (tokens)")
    ax.set_ylabel(r"$\theta$ (s)")
    ax.set_title("(a) Adaptive TTFT saving vs StaticSemantic")
    for i, theta in enumerate(THETAS):
        for j, c0 in enumerate(C0S):
            if not np.isfinite(grid[i, j]):
                continue
            ax.text(j, i, f"{grid[i, j]:.0f}", ha="center", va="center", fontsize=8,
                    color="white" if abs(grid[i, j]) > 0.55 * vmax else "black")
            if hatch[i, j]:
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False, hatch="///", edgecolor="0.35", linewidth=0.0))
            if c0 == 1024 and theta == 30:
                ax.add_patch(plt.Rectangle((j - 0.48, i - 0.48), 0.96, 0.96, fill=False, edgecolor="black", linewidth=1.6))
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label=r"$\Delta$TTFT (ms)")

    ax = axes[1]
    styles = {
        "RateFIFO": dict(marker="o", linestyle="--", color="#7a7a7a", label="RateFIFO"),
        "StaticSemantic": dict(marker="s", linestyle="-.", color="#1f4e79", label="StaticSemantic"),
        "Adaptive": dict(marker="D", linestyle="-", color="#b35c1e", label="Adaptive"),
    }
    by_pol = defaultdict(list)
    for row in panel_b:
        by_pol[row["policy"]].append(row)
    for policy, rows in by_pol.items():
        rows = sorted(rows, key=lambda item: int(item["prefix_length"]))
        xs = [int(row["prefix_length"]) for row in rows if int(row["n_reps"]) > 0]
        ys = [float(row["ttft_mean_ms"]) for row in rows if int(row["n_reps"]) > 0]
        yerr = [float(row["ttft_ci95_ms"]) for row in rows if int(row["n_reps"]) > 0]
        if not xs:
            continue
        ax.errorbar(xs, ys, yerr=yerr, capsize=3, **styles.get(policy, dict(marker="o", label=policy)))
    ax.set_xlabel("Reusable prefix length (tokens)")
    ax.set_ylabel("Mean TTFT (ms)")
    ax.set_title("(b) Gain vs reusable context")
    ax.set_xticks(LENGTHS)
    ax.legend(frameon=False, fontsize=8)
    ax.grid(True, axis="y", linewidth=0.4, alpha=0.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=160)
    plt.close(fig)


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


def main() -> None:
    cells = load_cells()
    panel_a = panel_a_table(cells)
    panel_b = panel_b_table(cells)
    write_csv(ROOT / "summary" / "panel_a_cells.csv", panel_a)
    write_csv(ROOT / "summary" / "panel_b_cells.csv", panel_b)
    write_csv(ROOT / "summary" / "all_sensitivity_cells.csv", cells)
    draw_figure(panel_a, panel_b, ROOT / "figures" / "fig_sensitivity_two_panel.pdf")
    paper = next((row for row in panel_a if int(row["paper_point"]) == 1), None)
    print({
        "n_source_cells": len(cells),
        "panel_a_rows": len(panel_a),
        "panel_b_rows": len(panel_b),
        "paper_point_delta_ttft_ms": None if paper is None else paper["delta_ttft_ms_mean"],
        "figure": str(ROOT / "figures" / "fig_sensitivity_two_panel.pdf"),
    })


if __name__ == "__main__":
    main()
