#!/usr/bin/env python3
"""Join F6-P1 and F6-P2 under one confirmation rule. Does not rerun GPU work.

ENGINE_CONFIRMED means harness in-flight equals vllm:num_requests_running on
every worker and num_requests_waiting is 0. Means are over those snapshots.
Repeated samples of one load vector are not treated as extra seeds.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/byh/B02/analysis/fig6_reconstruction")
P1_SNAP = ROOT / "f6_p1/observed_worker_snapshots.csv"
P1_ROUTE = ROOT / "f6_p1/counterfactual_routing.csv"
P2_ROUTE = ROOT / "f6_p2/counterfactual_routing.csv"
OUT = ROOT / "f6_p2/f6_p1_p2_comparison.csv"
PLOT = ROOT / "f6_p2/deltaload_regret_diagnostic.svg"

VISIBLE = (0, 1)
HIDDEN = (2, 3)


def ints(text: str) -> list[int]:
    return [int(part) for part in text.split(";") if part != ""]


def delta_load(loads: list[int]) -> int:
    return min(loads[index] for index in VISIBLE) - min(loads[index] for index in HIDDEN)


def p1_confirmation() -> dict[str, str]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in csv.DictReader(P1_SNAP.open()):
        grouped[row["snapshot_id"]].append(row)
    labels = {}
    for snapshot_id, rows in grouped.items():
        rows.sort(key=lambda item: int(item["worker"]))
        match = all(
            int(row["harness_inflight"]) == int(float(row["vllm_num_requests_running"]))
            and int(float(row["vllm_num_requests_waiting"])) == 0
            for row in rows
        )
        labels[snapshot_id] = "ENGINE_CONFIRMED" if match else "HARNESS_ONLY"
    return labels


def condition_of(source: str, cell: str, scenario: str) -> str | None:
    if source == "F6-P1" and scenario == "imbalanced":
        return "F6-P1 Forward"
    if source == "F6-P1" and scenario == "balanced":
        return "F6-P1 Forward idle"
    if cell == "A-reverse" and scenario == "ramp":
        return "F6-P2 Reverse"
    if cell == "B-interleaved" and scenario == "ramp":
        return "F6-P2 Interleaved"
    if cell == "C-forward-seed2" and scenario == "ramp":
        return "F6-P2 Forward Reproduction"
    if source == "F6-P2" and scenario == "idle":
        return "F6-P2 idle"
    return None


def load_points() -> list[dict]:
    points = []
    labels = p1_confirmation()
    # P1 balanced decisions share one poll. Keep one physical snapshot, rr=0.
    seen_balanced = False
    for row in csv.DictReader(P1_ROUTE.open()):
        if row["view"] != "krep2":
            continue
        if row["scenario"] == "balanced":
            if seen_balanced or row["rr"] != "0":
                continue
            seen_balanced = True
        loads = ints(row["loads"])
        points.append({
            "condition": condition_of("F6-P1", "", row["scenario"]),
            "cell": "F6-P1",
            "seed": "1",
            "snapshot_id": row["snapshot_id"],
            "confirmation": labels[row["snapshot_id"]],
            "loads": row["loads"],
            "delta_load": delta_load(loads),
            "regret_ms": float(row["regret_vs_full_ms"]),
            "best_owner_hidden": int(row["best_owner_hidden"]),
            "selected_changed": int(row["selected_changed_vs_full"]),
            "forwarded_ads": int(row["forwarded_ads"]),
            "dropped_ads": int(row["dropped_ads"]),
            "full_is_model_argmin": int(row["full_is_model_argmin"]),
        })
    if P2_ROUTE.exists():
        for row in csv.DictReader(P2_ROUTE.open()):
            if row["view"] != "krep2":
                continue
            condition = condition_of("F6-P2", row["cell"], row["scenario"])
            if condition is None or condition.endswith(" idle") or "idle" in condition and row["cell"] not in condition:
                pass
            points.append({
                "condition": condition,
                "cell": row["cell"],
                "seed": row["seed"],
                "snapshot_id": row["snapshot_id"],
                "confirmation": row["confirmation"],
                "loads": row["loads"],
                "delta_load": int(row["delta_load"]),
                "regret_ms": float(row["regret_vs_full_ms"]),
                "best_owner_hidden": int(row["best_owner_hidden"]),
                "selected_changed": int(row["selected_changed_vs_full"]),
                "forwarded_ads": int(row["forwarded_ads"]),
                "dropped_ads": int(row["dropped_ads"]),
                "full_is_model_argmin": int(row["full_is_model_argmin"]),
            })
    return points


def summarize(points: list[dict]) -> list[dict]:
    order = [
        "F6-P1 Forward",
        "F6-P1 Forward idle",
        "F6-P2 Reverse",
        "F6-P2 Interleaved",
        "F6-P2 Forward Reproduction",
        "F6-P2 idle",
    ]
    rows = []
    for condition in order:
        group = [item for item in points if item["condition"] == condition]
        if not group:
            continue
        confirmed = [item for item in group if item["confirmation"] == "ENGINE_CONFIRMED"]
        harness = [item for item in group if item["confirmation"] == "HARNESS_ONLY"]
        unique = {item["loads"] for item in confirmed}
        mean_regret = sum(item["regret_ms"] for item in confirmed) / len(confirmed) if confirmed else ""
        positive = sum(item["regret_ms"] > 0 for item in confirmed) / len(confirmed) if confirmed else ""
        mean_delta = sum(item["delta_load"] for item in confirmed) / len(confirmed) if confirmed else ""
        rows.append({
            "condition": condition,
            "n_cells": len({item["cell"] for item in group}),
            "n_snapshots": len(group),
            "n_engine_confirmed": len(confirmed),
            "n_harness_only": len(harness),
            "n_unique_confirmed_load_vectors": len(unique),
            "mean_model_regret_ms": mean_regret,
            "positive_regret_rate": positive,
            "mean_delta_load": mean_delta,
            "best_owner_hidden_rate": (
                sum(item["best_owner_hidden"] for item in confirmed) / len(confirmed) if confirmed else ""
            ),
            "selected_change_rate": (
                sum(item["selected_changed"] for item in confirmed) / len(confirmed) if confirmed else ""
            ),
            "ads_forwarded_mean": (
                sum(item["forwarded_ads"] for item in confirmed) / len(confirmed) if confirmed else ""
            ),
            "ads_dropped_mean": (
                sum(item["dropped_ads"] for item in confirmed) / len(confirmed) if confirmed else ""
            ),
            "full_is_argmin_rate": (
                sum(item["full_is_model_argmin"] for item in confirmed) / len(confirmed) if confirmed else ""
            ),
        })
    return rows


def write_svg(points: list[dict]) -> None:
    confirmed = [item for item in points if item["confirmation"] == "ENGINE_CONFIRMED" and item["condition"]]
    colors = {
        "F6-P1 Forward": "#1b4f72",
        "F6-P1 Forward idle": "#85929e",
        "F6-P2 Reverse": "#196f3d",
        "F6-P2 Interleaved": "#b9770e",
        "F6-P2 Forward Reproduction": "#6c3483",
        "F6-P2 idle": "#85929e",
    }
    width, height = 640, 420
    left, right, top, bottom = 70, 20, 30, 70
    xs = [item["delta_load"] for item in confirmed] or [0]
    ys = [item["regret_ms"] for item in confirmed] or [0]
    xmin, xmax = min(xs + [-1]), max(xs + [1])
    ymin, ymax = min(ys + [0]), max(ys + [2])
    if xmin == xmax:
        xmin, xmax = xmin - 1, xmax + 1
    if ymin == ymax:
        ymin, ymax = ymin - 1, ymax + 1

    def px(value: float) -> float:
        return left + (value - xmin) / (xmax - xmin) * (width - left - right)

    def py(value: float) -> float:
        return top + (ymax - value) / (ymax - ymin) * (height - top - bottom)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="70" y="18" font-size="13" font-family="sans-serif">Internal diagnostic, not Fig. 6. Engine-confirmed snapshots only. Model regret, not TTFT.</text>',
        f'<line x1="{left}" y1="{py(0)}" x2="{width-right}" y2="{py(0)}" stroke="#ddd"/>',
        f'<line x1="{px(0)}" y1="{top}" x2="{px(0)}" y2="{height-bottom}" stroke="#ddd"/>',
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#222"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#222"/>',
        f'<text x="{(left+width-right)/2}" y="{height-28}" text-anchor="middle" font-size="12" font-family="sans-serif">DeltaLoad = min(load 0,1) - min(load 2,3)</text>',
        f'<text x="18" y="{(top+height-bottom)/2}" transform="rotate(-90 18 {(top+height-bottom)/2})" font-size="12" font-family="sans-serif">Model routing regret (ms)</text>',
    ]
    jitter = defaultdict(int)
    for item in confirmed:
        key = (item["delta_load"], round(item["regret_ms"], 3), item["condition"])
        slot = jitter[key]
        jitter[key] += 1
        parts.append(
            f'<circle cx="{px(item["delta_load"]) + (slot % 5) * 4 - 8}" cy="{py(item["regret_ms"]) - (slot // 5) * 4}" r="4" '
            f'fill="{colors.get(item["condition"], "#333")}" fill-opacity="0.85"/>'
        )
    legend_y = height - 16
    x = 70
    for name, color in colors.items():
        if not any(item["condition"] == name for item in confirmed):
            continue
        parts.append(f'<rect x="{x}" y="{legend_y-8}" width="10" height="10" fill="{color}"/>')
        parts.append(f'<text x="{x+14}" y="{legend_y+2}" font-size="11" font-family="sans-serif">{name}</text>')
        x += 18 + len(name) * 6
    parts.append("</svg>")
    PLOT.write_text("\n".join(parts))


def main() -> None:
    points = load_points()
    rows = summarize(points)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    detail = OUT.with_name("f6_p1_p2_points.csv")
    with detail.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(points[0].keys()))
        writer.writeheader()
        writer.writerows(points)
    write_svg(points)
    for row in rows:
        print(row["condition"], "confirmed", row["n_engine_confirmed"], "mean_regret", row["mean_model_regret_ms"],
              "positive", row["positive_regret_rate"], "mean_delta", row["mean_delta_load"])


if __name__ == "__main__":
    main()
