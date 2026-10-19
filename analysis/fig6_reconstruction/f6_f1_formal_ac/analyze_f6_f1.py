#!/usr/bin/env python3
"""Summarize the Fig. 6(c) cells and write internal candidate figures."""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path("/home/byh/B02/analysis/fig6_reconstruction/f6_f1_formal_ac")


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def fnum(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}"


def summarize() -> list[dict]:
    rows = [row for row in read_csv(OUT / "f6_c_per_cell.csv") if row["status"] == "VALID"]
    by_overlap: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_overlap[f"{float(row['overlap']):.2f}"].append(row)
    summary = []
    for overlap in sorted(by_overlap, key=float):
        group = by_overlap[overlap]
        reduction = [float(row["reduction"]) for row in group]
        offered = [float(row["offered_advertisements"]) for row in group]
        forwarded0 = [float(row["forwarded_no_suppression"]) for row in group]
        forwarded2 = [float(row["forwarded_krep2"]) for row in group]
        drops = [float(row["replica_cap_drops"]) for row in group]
        pairs = [float(row["suppressed_owner_pairs"]) for row in group]
        visible = [float(row["mean_visible_owners_krep2"]) for row in group]
        sample = statistics.stdev(reduction) if len(reduction) > 1 else 0.0
        summary.append({
            "overlap": overlap,
            "n_seeds": len(group),
            "mean_offered_advertisements": fnum(statistics.fmean(offered), 3),
            "mean_forwarded_no_suppression": fnum(statistics.fmean(forwarded0), 3),
            "mean_forwarded_krep2": fnum(statistics.fmean(forwarded2), 3),
            "mean_replica_cap_drops": fnum(statistics.fmean(drops), 3),
            "mean_suppressed_owner_pairs": fnum(statistics.fmean(pairs), 3),
            "mean_visible_owners_krep2": fnum(statistics.fmean(visible), 3),
            "mean_advertisement_reduction": fnum(statistics.fmean(reduction)),
            "sample_stdev_reduction": fnum(sample),
            "min_reduction": fnum(min(reduction)),
            "max_reduction": fnum(max(reduction)),
            "seeds": ";".join(row["seed"] for row in group),
        })
    with (OUT / "f6_c_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)
    return summary


def style() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
        "axes.linewidth": 0.6,
        "lines.linewidth": 1.0,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })


def plot_a() -> None:
    rows = read_csv(OUT / "f6_a_per_rep.csv")
    reps = [int(row["rep"]) for row in rows]
    offered = [float(row["offered_updates"]) for row in rows]
    forwarded = [float(row["forwarded_updates"]) for row in rows]
    reduction = [100.0 * float(row["reduction"]) for row in rows]
    style()
    fig, ax = plt.subplots(figsize=(3.5, 2.45))
    width = 0.36
    positions = list(range(len(reps)))
    ax.bar([p - width / 2 for p in positions], offered, width, label="Offered", color="#4C4C4C")
    ax.bar([p + width / 2 for p in positions], forwarded, width, label="Forwarded", color="#9A9A9A")
    ax.set_xticks(positions, [f"R{rep}\n{value:.1f}%" for rep, value in zip(reps, reduction)])
    ax.set_ylabel("Advertisements")
    ax.set_xlabel("Rep and same-coverage reduction")
    ax.set_ylim(0, 460)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(OUT / "fig6a_candidate.pdf")
    fig.savefig(OUT / "fig6a_candidate.png", dpi=300)
    plt.close(fig)


def plot_c(summary: list[dict]) -> None:
    style()
    rows = [row for row in read_csv(OUT / "f6_c_per_cell.csv") if row["status"] == "VALID"]
    by_overlap: dict[float, list[dict]] = defaultdict(list)
    for row in rows:
        by_overlap[float(row["overlap"])].append(row)
    overlaps = sorted(by_overlap)
    means = [100.0 * float(item["mean_advertisement_reduction"]) for item in summary]
    lows = [100.0 * float(item["min_reduction"]) for item in summary]
    highs = [100.0 * float(item["max_reduction"]) for item in summary]
    yerr = [[mean - low for mean, low in zip(means, lows)], [high - mean for mean, high in zip(means, highs)]]
    fig, ax = plt.subplots(figsize=(3.5, 2.45))
    xs = [100.0 * overlap for overlap in overlaps]
    ax.errorbar(xs, means, yerr=yerr, fmt="-o", color="#1F4E79", ecolor="#1F4E79",
                elinewidth=0.8, capsize=2, markersize=3.5, label="Mean, min–max")
    for overlap, group in by_overlap.items():
        ax.scatter([100.0 * overlap] * len(group), [100.0 * float(row["reduction"]) for row in group],
                   s=12, color="#7F7F7F", zorder=3)
    for item, x in zip(summary, xs):
        ax.annotate(f"N={float(item['mean_offered_advertisements']):.0f}",
                    (x, float(item["mean_advertisement_reduction"]) * 100.0),
                    textcoords="offset points", xytext=(0, 7), ha="center", fontsize=6)
    ax.set_xlabel("Replica overlap (%)")
    ax.set_ylabel("Advertisement reduction (%)")
    ax.set_xlim(-5, 80)
    ax.set_ylim(0, max(highs + [10]) + 12)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "fig6c_candidate_reduction.pdf")
    fig.savefig(OUT / "fig6c_candidate_reduction.png", dpi=300)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(3.5, 2.45))
    width = 0.36
    positions = list(range(len(summary)))
    no_sup = [float(item["mean_forwarded_no_suppression"]) for item in summary]
    krep = [float(item["mean_forwarded_krep2"]) for item in summary]
    ax.bar([p - width / 2 for p in positions], no_sup, width, label="No suppression", color="#4C4C4C")
    ax.bar([p + width / 2 for p in positions], krep, width, label="Krep=2", color="#1F4E79")
    ax.set_xticks(positions, [f"{100 * float(item['overlap']):.0f}%" for item in summary])
    ax.set_xlabel("Replica overlap")
    ax.set_ylabel("Forwarded advertisements")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "fig6c_candidate_forwarded.pdf")
    fig.savefig(OUT / "fig6c_candidate_forwarded.png", dpi=300)
    plt.close(fig)


def main() -> None:
    summary = summarize()
    plot_a()
    plot_c(summary)
    for row in summary:
        print(row)


if __name__ == "__main__":
    main()
