"""Plot the Quality vs Cost trade-off curve from the 4-policy cell summaries.

Reads results/cells/<policy>/summary.json and produces:
  fig1_quality_vs_traffic.png
  fig2_cache_hit_per_policy.png
  fig3_dispatch_latency_per_policy.png
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS = os.path.expanduser("~/B02/tradeoff_experiments/results")
OUT = os.path.join(RESULTS, "figures")
os.makedirs(OUT, exist_ok=True)

POLICIES_ORDERED = ["round-robin", "coarse", "rich", "sketch"]
POLICY_LABEL = {
    "round-robin": "Round-Robin",
    "coarse": "Coarse",
    "rich": "Rich",
    "sketch": "Sketch",
}
POLICY_COLOR = {
    "round-robin": "#888888",
    "coarse": "#4477AA",
    "rich": "#CC6677",
    "sketch": "#117733",
}


def load_summaries():
    out = {}
    for p in POLICIES_ORDERED:
        path = os.path.join(RESULTS, "cells", p, "summary.json")
        if os.path.exists(path):
            with open(path) as f:
                out[p] = json.load(f)
    return out


def fig_quality_vs_traffic(summaries):
    """The main trade-off plot: cache hit rate (quality) vs state traffic (cost)."""
    fig, ax = plt.subplots(figsize=(9, 6))
    for p in POLICIES_ORDERED:
        if p not in summaries:
            continue
        s = summaries[p]
        ax.scatter(s["state_traffic_Bps"], s["cache_hit_rate"] * 100,
                   s=200, color=POLICY_COLOR[p], label=POLICY_LABEL[p],
                   edgecolors="black", linewidth=1.5, zorder=3)
    ax.set_xscale("log")
    ax.set_xlabel("State traffic (B/s, log scale) — COST axis")
    ax.set_ylabel("Cache hit rate (%) — QUALITY axis")
    ax.set_title("B02 Trade-off: Dispatch Quality vs State Cost (4 policies, 10 Hz)")
    ax.legend(loc="lower right", fontsize=11)
    ax.grid(True, which="both", alpha=0.3)
    # Annotate each point
    for p in POLICIES_ORDERED:
        if p not in summaries:
            continue
        s = summaries[p]
        ax.annotate(f"{s['cache_hit_rate']*100:.1f}%\n{s['state_traffic_Bps']:.0f} B/s",
                    (s["state_traffic_Bps"], s["cache_hit_rate"]*100),
                    textcoords="offset points", xytext=(10, 5), fontsize=8)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig1_quality_vs_traffic.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


def fig_dispatch_latency(summaries):
    fig, ax = plt.subplots(figsize=(9, 6))
    xs = list(range(len(POLICIES_ORDERED)))
    p50 = [summaries[p]["dispatch_decision_p50"] for p in POLICIES_ORDERED if p in summaries]
    p95 = [summaries[p]["dispatch_decision_p95"] for p in POLICIES_ORDERED if p in summaries]
    p99 = [summaries[p]["dispatch_decision_p99"] for p in POLICIES_ORDERED if p in summaries]
    width = 0.25
    ax.bar([x - width for x in xs], p50, width, label="p50", color="#88ccee")
    ax.bar(xs, p95, width, label="p95", color="#4477aa")
    ax.bar([x + width for x in xs], p99, width, label="p99", color="#223a5e")
    ax.set_xticks(xs)
    ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES_ORDERED if p in summaries])
    ax.set_ylabel("Dispatch decision latency (us)")
    ax.set_title("Dispatch Decision Time by Policy (lower = lower cost)")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig3_dispatch_latency.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


def fig_state_size(summaries):
    fig, ax = plt.subplots(figsize=(9, 6))
    xs = list(range(len(POLICIES_ORDERED)))
    avg = [summaries[p]["state_size_avg"] for p in POLICIES_ORDERED if p in summaries]
    p95 = [summaries[p]["state_size_p95"] for p in POLICIES_ORDERED if p in summaries]
    width = 0.35
    ax.bar([x - width/2 for x in xs], avg, width, label="avg", color="#88ccee")
    ax.bar([x + width/2 for x in xs], p95, width, label="p95", color="#4477aa")
    ax.set_xticks(xs)
    ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES_ORDERED if p in summaries])
    ax.set_ylabel("State size (bytes per instance per update)")
    ax.set_title("State View Size by Policy (rich is 6× larger)")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    for i, p in enumerate(POLICIES_ORDERED):
        if p in summaries:
            ax.text(i, summaries[p]["state_size_p95"], f"{summaries[p]['state_size_p95']} B",
                    ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig2_state_size.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


if __name__ == "__main__":
    s = load_summaries()
    print(f"Loaded {len(s)} summaries")
    fig_quality_vs_traffic(s)
    fig_dispatch_latency(s)
    fig_state_size(s)