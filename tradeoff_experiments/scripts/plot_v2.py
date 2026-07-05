"""Plot the v2 trade-off results: 4 policies x 2 load conditions.

Generates:
  fig1_quality_vector_balanced.png  - radar chart of 5 quality dimensions, balanced
  fig2_quality_vector_imbalanced.png - radar chart, imbalanced
  fig3_cost_vs_quality_scatter.png  - traffic vs cache hit
  fig4_load_stdev_per_policy.png   - per-instance load balance
  fig5_ttft_p95_per_policy.png     - TTFT p95 per policy
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RESULTS = os.path.expanduser("~/B02/tradeoff_experiments/results_v2/cells")
OUT = os.path.expanduser("~/B02/tradeoff_experiments/results_v2/figures")
os.makedirs(OUT, exist_ok=True)

POLICIES = ["round-robin", "coarse", "rich", "sketch"]
POLICY_LABEL = {"round-robin": "Round-Robin", "coarse": "Coarse",
                "rich": "Rich", "sketch": "Sketch"}
POLICY_COLOR = {"round-robin": "#888888", "coarse": "#4477AA",
                "rich": "#CC6677", "sketch": "#117733"}


def load_all():
    out = {}
    for p in POLICIES:
        for lc in ("balanced", "imbalanced"):
            path = os.path.join(RESULTS, f"{p}_{lc}", "summary.json")
            if os.path.exists(path):
                with open(path) as f:
                    out[(p, lc)] = json.load(f)
    return out


# Normalize each quality metric to [0, 1] within the 8 cells (higher = better)
def normalize_quality(summaries, key, higher_is_better=True):
    vals = [s[key] for s in summaries.values()]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {k: 0.5 for k in summaries}
    def norm(s):
        v = (s[key] - lo) / (hi - lo)
        return v if higher_is_better else (1 - v)
    return {k: norm(s) for k, s in summaries.items()}


# For radar, we want 5 quality dimensions, all "higher = better"
#   1. cache_hit_rate        (higher is better)
#   2. 1/load_stdev          (lower stdev is better)
#   3. 1/ttft_p95            (lower TTFT is better)
#   4. 1/workflow_completion_p95  (lower is better)
#   5. sla_success_rate      (higher is better)

def build_radar(summaries, load_cond):
    """Build radar data for the 4 policies at a given load condition."""
    # filter to this load condition
    cells = {k: s for k, s in summaries.items() if k[1] == load_cond}
    # normalize across these 4 cells
    metrics = {
        "cache_hit_rate": (True, 0.05),  # higher is better
        "neg_load_stdev": (False, 0.5),  # we'll invert
        "neg_ttft_p95": (False, 1.0),
        "neg_workflow_completion_p95": (False, 1.0),
        "sla_success_rate": (True, 0.0),
    }
    # create per-metric score in [0, 1]
    scores = {p: {} for p in POLICIES}
    for p in POLICIES:
        s = cells.get((p, load_cond))
        if not s:
            continue
        for mkey, (higher_better, _) in metrics.items():
            if mkey == "neg_load_stdev":
                scores[p][mkey] = -s["load_stdev"]
            elif mkey == "neg_ttft_p95":
                scores[p][mkey] = -s["ttft_p95"]
            elif mkey == "neg_workflow_completion_p95":
                scores[p][mkey] = -s["workflow_completion_p95"]
            else:
                scores[p][mkey] = s[mkey]
    # normalize each metric across the 4 cells
    normalized = {p: {} for p in POLICIES}
    for mkey in metrics:
        vals = [scores[p][mkey] for p in POLICIES if mkey in scores[p]]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        for p in POLICIES:
            if mkey not in scores[p]:
                continue
            v = (scores[p][mkey] - lo) / (hi - lo) if hi > lo else 0.5
            normalized[p][mkey] = v
    return normalized


def fig_radar(summaries, load_cond, out_name):
    normalized = build_radar(summaries, load_cond)
    metrics = ["cache_hit_rate", "neg_load_stdev", "neg_ttft_p95",
               "neg_workflow_completion_p95", "sla_success_rate"]
    metric_labels = ["Cache hit\n(higher better)",
                     "Load balance\n(stdev lower=better)",
                     "TTFT p95\n(lower better)",
                     "Workflow p95\n(lower better)",
                     "SLA success\n(% steps <3s)"]
    angles = np.linspace(0, 2*np.pi, len(metrics), endpoint=False).tolist()
    angles += angles[:1]  # close the polygon
    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
    for p in POLICIES:
        if p not in normalized:
            continue
        vals = [normalized[p].get(m, 0) for m in metrics]
        vals += vals[:1]  # close
        ax.plot(angles, vals, label=POLICY_LABEL[p], color=POLICY_COLOR[p],
                linewidth=2, marker="o", markersize=6)
        ax.fill(angles, vals, color=POLICY_COLOR[p], alpha=0.10)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(metric_labels, fontsize=10)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["0.25", "0.50", "0.75", "1.00"], fontsize=8)
    ax.set_title(f"Quality vector — {load_cond} load", fontsize=13, pad=20)
    ax.legend(loc="lower right", bbox_to_anchor=(1.18, -0.05), fontsize=10)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT, out_name)
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"wrote {out_path}")


def fig_cost_vs_quality(summaries):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    for idx, lc in enumerate(["balanced", "imbalanced"]):
        ax = axes[idx]
        for p in POLICIES:
            s = summaries.get((p, lc))
            if not s:
                continue
            ax.scatter(s["state_traffic_Bps"], s["cache_hit_rate"]*100,
                       s=300, color=POLICY_COLOR[p], label=POLICY_LABEL[p],
                       edgecolors="black", linewidth=1.5, zorder=3)
            ax.annotate(f"{s['ttft_p95']:.0f}ms",
                        (s["state_traffic_Bps"], s["cache_hit_rate"]*100),
                        textcoords="offset points", xytext=(8, 5), fontsize=8)
        ax.set_xscale("log")
        ax.set_xlabel("State traffic (B/s, log)")
        ax.set_ylabel("Cache hit rate (%)")
        ax.set_title(f"Cost vs Quality (cache hit) — {lc}")
        ax.legend(loc="lower right", fontsize=9)
        ax.grid(True, which="both", alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig3_cost_vs_quality.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


def fig_load_stdev(summaries):
    fig, ax = plt.subplots(figsize=(9, 6))
    x = np.arange(len(POLICIES))
    width = 0.35
    bal = [summaries.get((p, "balanced"), {}).get("load_stdev", 0) for p in POLICIES]
    imb = [summaries.get((p, "imbalanced"), {}).get("load_stdev", 0) for p in POLICIES]
    ax.bar(x - width/2, bal, width, label="balanced", color="#88ccee")
    ax.bar(x + width/2, imb, width, label="imbalanced", color="#cc6677")
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES])
    ax.set_ylabel("Per-instance load stdev (lower = more balanced)")
    ax.set_title("Load balance quality by policy")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig4_load_stdev.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


def fig_ttft_p95(summaries):
    fig, ax = plt.subplots(figsize=(9, 6))
    x = np.arange(len(POLICIES))
    width = 0.35
    bal = [summaries.get((p, "balanced"), {}).get("ttft_p95", 0) for p in POLICIES]
    imb = [summaries.get((p, "imbalanced"), {}).get("ttft_p95", 0) for p in POLICIES]
    bal_c = [summaries.get((p, "balanced"), {}).get("cache_hit_rate", 0) * 100 for p in POLICIES]
    imb_c = [summaries.get((p, "imbalanced"), {}).get("cache_hit_rate", 0) * 100 for p in POLICIES]
    bars1 = ax.bar(x - width/2, bal, width, label="TTFT p95 (balanced)", color="#88ccee")
    bars2 = ax.bar(x + width/2, imb, width, label="TTFT p95 (imbalanced)", color="#cc6677")
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES])
    ax.set_ylabel("TTFT p95 (ms, lower = better)")
    ax.set_title("TTFT p95 by policy × load condition")
    # Annotate with cache hit %
    for i, p in enumerate(POLICIES):
        ax.text(i - width/2, bal[i] + 10, f"cache {bal_c[i]:.1f}%",
                ha="center", va="bottom", fontsize=8, color="#225588")
        ax.text(i + width/2, imb[i] + 10, f"cache {imb_c[i]:.1f}%",
                ha="center", va="bottom", fontsize=8, color="#883344")
    ax.legend(loc="upper left")
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT, "fig5_ttft_p95.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


if __name__ == "__main__":
    s = load_all()
    print(f"Loaded {len(s)} cells")
    fig_radar(s, "balanced", "fig1_radar_balanced.png")
    fig_radar(s, "imbalanced", "fig2_radar_imbalanced.png")
    fig_cost_vs_quality(s)
    fig_load_stdev(s)
    fig_ttft_p95(s)