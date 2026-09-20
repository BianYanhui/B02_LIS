#!/usr/bin/env python3
"""Two-panel control-plane anchor figure plus CSV tables for the letter."""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path("/home/byh/B02/supplemental_20260920_controlplane")
NS = [4, 16, 32, 64, 128]
MU = 200.0
SYNC_S = 1.0


def read_csv(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def fnum(row: dict, key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def mean_ci(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    arr = np.asarray(values, dtype=float)
    mu = float(arr.mean())
    if len(arr) < 2:
        return mu, 0.0
    se = float(arr.std(ddof=1) / math.sqrt(len(arr)))
    return mu, 1.96 * se


def load_valid(path: Path) -> list[dict]:
    return [row for row in read_csv(path) if row.get("status") == "VALID"]


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    calibrate = load_valid(ROOT / "summary" / "cells_calibrate.csv")
    fanin_cells = load_valid(ROOT / "summary" / "cells_fanin.csv")
    cells = calibrate + fanin_cells
    ROOT.joinpath("figures").mkdir(exist_ok=True)
    ROOT.joinpath("summary").mkdir(exist_ok=True)

    # Prefer Ideal upsert rate from unconstrained calibration, not smoke or fan-in.
    ideals = [row for row in calibrate if row.get("policy") == "Ideal"]
    live_n4 = [row for row in calibrate if row.get("policy") != "Ideal"]
    source = ideals[0] if ideals else (calibrate[0] if calibrate else (cells[0] if cells else None))
    l4 = fnum(source, "upserts_per_s") if source else 3.0
    prefixes = fnum(source, "source_unique_prefixes_at_end", 50.0) if source else 50.0
    apply_p50 = fnum(source, "apply_upsert_p50_us") if source else 0.0
    apply_p95 = fnum(source, "apply_upsert_p95_us") if source else 0.0
    delay_p95 = fnum(source, "ad_delivery_delay_p95_s") if source and source.get("policy") != "Ideal" else (
        fnum(live_n4[0], "ad_delivery_delay_p95_s") if live_n4 else 0.0
    )
    r_sync = (prefixes / max(l4, 1e-9)) / SYNC_S if l4 else 1.0

    anchor_rows = []
    for n in NS:
        offered = l4 * (n / 4.0) * (1.0 + r_sync)
        anchor_rows.append({
            "cluster_n": n,
            "L4_upserts_per_s": round(l4, 4),
            "r_sync": round(r_sync, 4),
            "L_n_frames_per_s": round(offered, 4),
            "mu_frames_per_s": MU,
            "rho_ctrl": round(offered / MU, 4),
            "rho_ctrl_ge_1": int(offered >= MU),
            "apply_upsert_p50_us": round(apply_p50, 4),
            "apply_upsert_p95_us": round(apply_p95, 4),
            "unconstrained_delay_p95_s": round(delay_p95, 4),
        })
    write_csv(ROOT / "summary" / "anchor_L_of_N.csv", anchor_rows)

    # Live measured offered rate and outcomes.
    live = [row for row in fanin_cells if row.get("policy") in {"RateFIFO", "StaticSemantic", "Adaptive"}]
    grouped: dict[tuple, list] = defaultdict(list)
    for row in live:
        key = (int(fnum(row, "cluster_n", 4)), row["policy"])
        grouped[key].append(row)

    fanin_rows = []
    delay_rows = []
    for (n, policy), group in sorted(grouped.items()):
        ttft, ttft_ci = mean_ci([fnum(r, "ttft_mean_ms") for r in group])
        vm, vm_ci = mean_ci([fnum(r, "dispatcher_view_missing_at_dispatch_rate") for r in group])
        delay, delay_ci = mean_ci([fnum(r, "ad_delivery_delay_p95_s") for r in group])
        offered, offered_ci = mean_ci([
            (fnum(r, "upserts_generated") + fnum(r, "source_tombstones_sent") + fnum(r, "fanin_frames_sent"))
            / max(fnum(r, "cell_active_s"), 1e-9)
            for r in group
        ])
        q, q_ci = mean_ci([fnum(r, "relay_max_queue") for r in group])
        ewma, ewma_ci = mean_ci([fnum(r, "ad_delivery_delay_mean_s") for r in group])
        fanin_rows.append({
            "cluster_n": n, "policy": policy, "n_reps": len(group),
            "ttft_mean_ms": ttft, "ttft_ci95_ms": ttft_ci,
            "view_missing_mean": vm, "view_missing_ci95": vm_ci,
            "offered_frames_per_s": offered, "offered_ci95": offered_ci,
            "relay_max_queue_mean": q, "ad_delivery_delay_p95_s": delay,
            "rho_ctrl_measured": offered / MU if MU else 0.0,
        })
        delay_rows.append({
            "cluster_n": n, "policy": policy, "n_reps": len(group),
            "path_delay_mean_s": ewma, "path_delay_mean_ci95": ewma_ci,
            "path_delay_p95_s": delay, "path_delay_p95_ci95": delay_ci,
            "relay_max_queue_mean": q, "relay_max_queue_ci95": q_ci,
            "apply_upsert_p50_us": mean_ci([fnum(r, "apply_upsert_p50_us") for r in group])[0],
            "apply_upsert_p95_us": mean_ci([fnum(r, "apply_upsert_p95_us") for r in group])[0],
        })
    # Keep harness per-cell cells_fanin.csv; write means for the letter tables.
    write_csv(ROOT / "summary" / "cells_fanin_means.csv", fanin_rows)
    write_csv(ROOT / "summary" / "delay_breakdown.csv", delay_rows)

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.15), dpi=160)
    ax = axes[0]
    xs = [row["cluster_n"] for row in anchor_rows]
    ys = [row["L_n_frames_per_s"] for row in anchor_rows]
    ax.plot(xs, ys, marker="o", color="#1f4e79", label="L(N) from 4-GPU yield")
    ax.axhline(MU, color="#c45911", linestyle="--", label=f"μ={int(MU)} frames/s")
    ax.axhline(MU, color="#c45911", alpha=0.12)
    cross = next((row["cluster_n"] for row in anchor_rows if row["rho_ctrl_ge_1"]), None)
    if cross:
        ax.axvline(cross, color="#7f7f7f", linestyle=":", linewidth=1.0)
        ax.text(cross, MU * 1.05, r"$\rho_{ctrl}=1$", fontsize=8, color="#7f7f7f")
    ax.set_xlabel("Logical workers N sharing the gateway")
    ax.set_ylabel("Offered metadata (frames/s)")
    ax.set_title("(a) Why a real site enters overload")
    ax.set_xticks(NS)
    ax.legend(frameon=False, fontsize=8)
    ax.grid(True, alpha=0.25)

    ax = axes[1]
    ns_live = sorted({int(row["cluster_n"]) for row in fanin_rows})
    styles = {
        "RateFIFO": ("#7f7f7f", "s", "--"),
        "StaticSemantic": ("#2e75b6", "o", "-"),
        "Adaptive": ("#c45911", "D", "-"),
    }
    if ns_live:
        for policy, (color, marker, ls) in styles.items():
            series = [row for row in fanin_rows if row["policy"] == policy]
            if not series:
                continue
            series = sorted(series, key=lambda row: row["cluster_n"])
            ax.errorbar(
                [row["cluster_n"] for row in series],
                [row["ttft_mean_ms"] for row in series],
                yerr=[row["ttft_ci95_ms"] for row in series],
                color=color, marker=marker, linestyle=ls, label=policy, capsize=3, linewidth=1.4,
            )
        ax.set_xlabel("Logical workers N")
        ax.set_ylabel("Mean TTFT (ms)")
        ax.set_title("(b) Serving latency vs control-plane fan-in")
        ax.set_xticks(ns_live)
        ax.legend(frameon=False, fontsize=8)
        ax.grid(True, alpha=0.25)
    else:
        ax.text(0.5, 0.5, "fan-in grid not run yet", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()

    fig.tight_layout()
    fig.savefig(ROOT / "figures" / "fig_controlplane_two_panel.pdf")
    fig.savefig(ROOT / "figures" / "fig_controlplane_two_panel.png")
    plt.close(fig)
    print({
        "n_cells": len(cells),
        "L4": l4,
        "r_sync": r_sync,
        "anchor_rows": len(anchor_rows),
        "fanin_rows": len(fanin_rows),
        "apply_upsert_p50_us": apply_p50,
        "figure": str(ROOT / "figures" / "fig_controlplane_two_panel.pdf"),
    })


if __name__ == "__main__":
    main()
