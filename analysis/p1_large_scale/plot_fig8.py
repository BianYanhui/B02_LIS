#!/usr/bin/env python3
"""Aggregate P1 summaries and draw Fig. 8. No smoothing and no dropped seeds."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

OUT = Path("/home/byh/B02/analysis/p1_large_scale")
RUNS = OUT / "runs"
# df = 2, two-sided 95%. Three seeds.
T_95 = 4.302652729749464
MAIN = ("FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "KVSelect")
STYLE = {
    "FullSync": ("#4C4C4C", "o"),
    "RateFIFO": ("#0072B2", "s"),
    "LatestOnly": ("#E69F00", "D"),
    "AgeCov-Greedy": ("#009E73", "^"),
    "KVSelect": ("#D55E00", "P"),
    "BoundedFIFO": ("#56B4E9", "v"),
    "BoundedPrio": ("#CC79A7", "X"),
    "BoundedSemantic": ("#000000", "h"),
}


def mean_ci(values: list[float]) -> tuple[float, float]:
    count = len(values)
    mean = sum(values) / count
    if count < 2:
        return mean, 0.0
    var = sum((value - mean) ** 2 for value in values) / (count - 1)
    return mean, T_95 * math.sqrt(var / count)


def load() -> list[dict]:
    rows = []
    for path in sorted(RUNS.glob("*/summary.json")):
        rows.append(json.loads(path.read_text()))
    return rows


def write_aggregate(rows: list[dict]) -> None:
    if not rows:
        return
    keys = sorted({key for row in rows for key in row if key != "drops"})
    with (OUT / "aggregate.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys + ["drops_json"], lineterminator="\n")
        writer.writeheader()
        for row in rows:
            flat = {key: row.get(key, "") for key in keys}
            flat["drops_json"] = json.dumps(row.get("drops", {}), sort_keys=True)
            writer.writerow(flat)


def _font() -> None:
    path = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
    font_manager.fontManager.addfont(path)
    matplotlib.rcParams.update({
        "font.family": "Liberation Serif",
        "font.size": 8,
        "axes.labelsize": 8,
        "legend.fontsize": 6,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "axes.linewidth": 0.6,
    })


def _series(rows: list[dict], ykey: str) -> dict[str, list[tuple[float, float, float]]]:
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in rows:
        if not row.get("balance_ok", False):
            continue
        value = row.get(ykey)
        if value is None:
            continue
        grouped[(row["policy"], int(row["n_workers"]))].append(float(value))
    out: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for (policy, n_workers), values in sorted(grouped.items()):
        mean, half = mean_ci(values)
        out[policy].append((n_workers, mean, half))
    return out


def _draw(path: Path, series: dict[str, list[tuple[float, float, float]]], ylabel: str, yscale: str, policies: tuple[str, ...]) -> None:
    _font()
    fig, ax = plt.subplots(figsize=(3.5, 2.7))
    for policy in policies:
        points = series.get(policy, [])
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        half = [point[2] for point in points]
        # The Student-t interval can cross 0 when three seeds disagree.
        # The whisker is drawn only down to 0. The mean is unchanged.
        lower = [min(value, err) for value, err in zip(ys, half)]
        color, marker = STYLE[policy]
        ax.errorbar(xs, ys, yerr=[lower, half], color=color, marker=marker, markersize=4, linewidth=1.0, capsize=2, label=policy)
    ax.set_xscale("log")
    ax.set_yscale(yscale)
    ax.set_xticks([4, 64, 256, 1024, 4096])
    ax.set_xticklabels(["4", "64", "256", "1024", "4096"])
    ax.set_xlabel("Logical workers")
    ax.set_ylabel(ylabel)
    ax.legend(frameon=False, loc="best")
    ax.grid(True, which="both", linewidth=0.3, alpha=0.4)
    fig.tight_layout()
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)


def main() -> None:
    rows = load()
    write_aggregate(rows)
    primary = [row for row in rows if row.get("experiment") == "A" and row.get("mbps") == 10]
    delay = _series(primary, "digest_p95_censored_s")
    fn = _series(primary, "decision_fn_rate")
    fn_pct = {policy: [(n, 100.0 * mean, 100.0 * half) for n, mean, half in points] for policy, points in fn.items()}
    _draw(OUT / "fig8a_scaling_delay.pdf", delay, "P95 coordination delay (s)", "log", MAIN)
    _draw(OUT / "fig8b_scaling_routing_fn.pdf", fn_pct, "Routing FN (%)", "linear", MAIN)
    # Experiment B is not Fig. 8. It is the pressure-matched companion.
    pressed = [row for row in rows if row.get("experiment") == "B" and row.get("rho_target") == 1.2]
    _draw(OUT / "figS_rho12_delay.pdf", _series(pressed, "digest_p95_censored_s"), "P95 coordination delay at rho 1.2 (s)", "linear", MAIN)
    pressed_fn = _series(pressed, "decision_fn_rate")
    pressed_pct = {policy: [(n, 100.0 * mean, 100.0 * half) for n, mean, half in points] for policy, points in pressed_fn.items()}
    _draw(OUT / "figS_rho12_routing_fn.pdf", pressed_pct, "Routing FN at rho 1.2 (%)", "linear", MAIN)
    pressure = [row for row in rows if row.get("experiment") == "C" and row.get("n_workers") == 4096]
    kseries: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in pressure:
        if row.get("balance_ok") and row.get("decision_fn_rate") is not None:
            grouped[(row["policy"], int(row["k"]))].append(float(row["decision_fn_rate"]))
    for (policy, k), values in sorted(grouped.items()):
        mean, half = mean_ci(values)
        kseries[policy].append((k, 100.0 * mean, 100.0 * half))
    _font()
    fig, ax = plt.subplots(figsize=(3.5, 2.7))
    for policy, points in kseries.items():
        color, marker = STYLE.get(policy, ("#333333", "o"))
        ys = [point[1] for point in points]
        half = [point[2] for point in points]
        lower = [min(value, err) for value, err in zip(ys, half)]
        ax.errorbar(
            [point[0] for point in points], ys, yerr=[lower, half],
            color=color, marker=marker, markersize=4, linewidth=1.0, capsize=2, label=policy,
        )
    ax.set_xticks([16, 32, 64, 128])
    ax.set_xlabel("K")
    ax.set_ylabel("Routing FN at 4096 workers (%)")
    ax.legend(frameon=False, fontsize=6)
    ax.grid(True, linewidth=0.3, alpha=0.4)
    fig.tight_layout()
    fig.savefig(OUT / "fig_k_sensitivity.pdf")
    fig.savefig(OUT / "fig_k_sensitivity.png", dpi=200)
    plt.close(fig)
    print(f"rows {len(rows)} primary {len(primary)} pressure {len(pressure)}")


if __name__ == "__main__":
    main()
