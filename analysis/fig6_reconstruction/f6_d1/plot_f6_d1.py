#!/usr/bin/env python3
"""Internal Fig. 6(d) candidate. Does not replace the paper figure."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import csv

OUT = Path("/home/byh/B02/analysis/fig6_reconstruction/f6_d1")
rows = list(csv.DictReader((OUT / "f6_d1_paired_results.csv").open()))
formal = [row for row in rows if row["phase"] == "formal"]
scenarios = ["D0", "D1", "D2"]
labels = [
    "D0 equal\n4096 on all",
    "D1 hidden\nhas 4096",
    "D2 visible\nhas 4096",
]

fig, ax = plt.subplots(figsize=(7.4, 5.0))
for index, scenario in enumerate(scenarios):
    deltas = [float(row["delta_ttft_ms"]) for row in formal if row["scenario"] == scenario]
    mean = sum(deltas) / len(deltas)
    ax.scatter([index] * len(deltas), deltas, s=36, color="#1f4e79", zorder=3)
    ax.plot([index - 0.18, index + 0.18], [mean, mean], color="#b85c38", lw=2.5, zorder=2)

ax.axhline(0, color="#666666", lw=0.8)
ax.set_xticks(range(3), labels)
ax.set_ylabel("Paired client TTFT: Krep=2 minus Full (ms)")
ax.set_title("Fig. 6(d) candidate — measured TTFT, load 0")
ax.set_xlim(-0.6, 2.6)
ax.annotate(
    "first formal pair, same worker\ncached 4096 both views\n1390 ms then 124 ms",
    xy=(0, -1266.5),
    xytext=(0.35, -900),
    fontsize=8,
    color="#333333",
    arrowprops=dict(arrowstyle="->", color="#666666"),
)
fig.text(
    0.08,
    0.012,
    "Four formal seeds. Dots are pairs; orange bars are means. Three other D0 dots sit on 0.\n"
    "D3 held-load deltas, 4072 ms and 5485 ms, are not on this axis.\n"
    "D1 model regret is 61.44 ms and is not this delay. Admission order is controlled.",
    fontsize=8,
    color="#333333",
)
fig.tight_layout(rect=(0, 0.10, 1, 1))
fig.savefig(OUT / "fig6d_candidate.png", dpi=160)
fig.savefig(OUT / "fig6d_candidate.pdf")
print("wrote", OUT / "fig6d_candidate.png")
