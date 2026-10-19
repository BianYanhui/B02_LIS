#!/usr/bin/env python3
"""Candidate figure only. Does not replace the paper figure."""
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

base = Path("/home/byh/B02/analysis/fig6_reconstruction/f6_a2")
rows = [row for row in csv.DictReader((base / "f6_a2_summary.csv").open()) if row["phase"] == "formal"]
conditions = ["natural", "controlled"]
fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.4))

ax = axes[0]
for index, condition in enumerate(conditions):
    vals = [float(row["signaling_reduction"]) for row in rows if row["condition"] == condition]
    ax.scatter([index] * len(vals), vals, color="#1f4e79", zorder=3)
    ax.plot([index - 0.12, index + 0.12], [sum(vals) / len(vals)] * 2, color="#1f4e79", lw=2)
ax.set_xticks([0, 1], ["Natural", "Controlled\ninflight window"])
ax.set_ylim(-0.05, 0.8)
ax.set_ylabel("Forwarded-update reduction")
ax.set_title("Same captured stream")

ax = axes[1]
width = 0.32
for index, condition in enumerate(conditions):
    off = [float(row["visibility_p95_off"]) for row in rows if row["condition"] == condition]
    on = [float(row["visibility_p95_on"]) for row in rows if row["condition"] == condition]
    ax.bar(index - width / 2, sum(off) / len(off), width, color="#9aa0a6", label="Merge off" if index == 0 else None)
    ax.bar(index + width / 2, sum(on) / len(on), width, color="#1f4e79", label="Merge on" if index == 0 else None)
    ax.scatter([index - width / 2] * len(off), off, color="black", s=12, zorder=3)
    ax.scatter([index + width / 2] * len(on), on, color="black", s=12, zorder=3)
ax.set_xticks([0, 1], ["Natural", "Controlled\ninflight window"])
ax.set_ylabel("P95 coverage-visibility delay (s)")
ax.set_title("Replay clock, not TTFT")
ax.legend(frameon=False)

fig.suptitle("Fig. 6(a) candidate — true extension supersession", fontsize=11)
fig.tight_layout()
fig.savefig(base / "fig6a_candidate.png", dpi=160)
fig.savefig(base / "fig6a_candidate.pdf")
print("wrote", base / "fig6a_candidate.png")
