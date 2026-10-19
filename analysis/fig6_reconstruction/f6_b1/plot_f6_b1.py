#!/usr/bin/env python3
"""Internal Fig. 6(b) candidate. Does not replace the paper figure."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import csv

OUT = Path("/home/byh/B02/analysis/fig6_reconstruction/f6_b1")
rows = list(csv.DictReader((OUT / "f6_b1_per_cell.csv").open()))
formal = [
    row for row in rows
    if row["probe_is_frozen"] == "1" and row["seed"] != "2026101919"
]
conditions = ["natural", "controlled"]
cond_labels = ["Natural", "Controlled\nACK window"]
policies = [("0", "FIFO", "#1f4e79"), ("1", "Priority", "#b85c38")]


def cells(condition: str, priority: str) -> list[dict]:
    return [row for row in formal if row["condition"] == condition and row["priority"] == priority]


fig, axes = plt.subplots(1, 2, figsize=(7.2, 4.8))

ax = axes[0]
for offset, (priority, label, color) in zip((-0.16, 0.16), policies):
    rates = []
    for index, condition in enumerate(conditions):
        group = cells(condition, priority)
        rate = sum(int(row["selected_worker"] == "0" and row["counterfactual_selected"] == "1") for row in group) / len(group)
        rates.append(rate)
        ax.scatter(
            [index + offset] * len(group),
            [1 if row["selected_worker"] == "0" and row["counterfactual_selected"] == "1" else 0 for row in group],
            s=28, color=color, zorder=3,
        )
    ax.bar(
        [index + offset for index in range(2)],
        rates, width=0.28, color=color, alpha=0.85, label=label, zorder=2,
    )
ax.set_xticks(range(2), cond_labels)
ax.set_ylim(-0.05, 1.18)
ax.set_ylabel("Decision-level invalid routes")
ax.set_title("Read-only choose")
ax.legend(frameon=False, fontsize=8)

ax = axes[1]
for offset, (priority, label, color) in zip((-0.16, 0.16), policies):
    means = []
    for index, condition in enumerate(conditions):
        vals = [float(row["stale_duration_s"]) for row in cells(condition, priority)]
        means.append(sum(vals) / len(vals))
        ax.scatter([index + offset] * len(vals), vals, s=28, color=color, zorder=3)
    ax.bar(
        [index + offset for index in range(2)],
        means, width=0.28, color=color, alpha=0.85, label=label, zorder=2,
    )
ax.set_xticks(range(2), cond_labels)
ax.set_ylabel("Constructed stale-positive duration (s)")
ax.set_title("Detection plus replay delay")
ax.legend(frameon=False, fontsize=8)

fig.suptitle("Fig. 6(b) candidate — invalidation priority", fontsize=12)
fig.text(
    0.08, 0.012,
    "Formal seeds 2026101920–22. One read-only choose per cell, 2.372 s after enqueue.\n"
    "Natural left the queue empty. Controlled held one frame and delayed each ACK by 0.792 s, frozen before these seeds.\n"
    "Duration adds the observed reset-to-clear interval (~5 ms) to the timed-replay apply delay.\n"
    "Executed requests saw cached tokens 0 on the stale route and 4096 after the tombstone. They are not these bars.",
    fontsize=8, color="#333333",
)
fig.tight_layout(rect=(0, 0.16, 1, 0.94))
fig.savefig(OUT / "fig6b_candidate.png", dpi=160)
fig.savefig(OUT / "fig6b_candidate.pdf")
print("wrote", OUT / "fig6b_candidate.png")
