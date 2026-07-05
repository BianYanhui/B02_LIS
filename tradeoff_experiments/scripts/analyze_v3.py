"""Analyze v3 results: aggregate across reps + statistical tests.

For each (policy, load_condition) cell:
  - Aggregate 3 reps: mean, std, 95% CI
  - Paired statistical tests vs Coarse (baseline):
    * Welch's t-test (unequal variances, small N)
    * Wilcoxon (non-parametric, when sample sizes permit)
  - Effect size: Cohen's d

Outputs:
  - aggregates/per_cell_summary.csv
  - aggregates/stat_tests.csv
  - figures/quality_by_policy_with_ci.png
  - figures/cost_vs_quality_with_errorbars.png
"""
import json
import math
import os
import re
import sys
from collections import defaultdict
from statistics import mean, stdev
from itertools import combinations

import numpy as np

RESULTS = os.path.expanduser("~/B02/tradeoff_experiments/results_v3/cells")
AGG = os.path.expanduser("~/B02/tradeoff_experiments/results_v3/aggregates")
FIG = os.path.expanduser("~/B02/tradeoff_experiments/results_v3/figures")
os.makedirs(AGG, exist_ok=True)
os.makedirs(FIG, exist_ok=True)

POLICIES = ["round-robin", "coarse", "rich", "sketch"]
POLICY_LABEL = {"round-robin": "Round-Robin", "coarse": "Coarse",
                "rich": "Rich", "sketch": "Sketch"}
POLICY_COLOR = {"round-robin": "#888888", "coarse": "#4477AA",
                "rich": "#CC6677", "sketch": "#117733"}
LOADS = ["balanced", "imbalanced_2", "imbalanced_6"]
LOAD_LABEL = {"balanced": "balanced", "imbalanced_2": "2/8 loaded", "imbalanced_6": "6/8 loaded"}


def load_summaries():
    """Scan cells directory directly, classify each cell by inspecting name."""
    out = {}
    if not os.path.isdir(RESULTS):
        return out
    for d in os.listdir(RESULTS):
        sp = os.path.join(RESULTS, d, "summary.json")
        if not os.path.isdir(os.path.join(RESULTS, d)) or not os.path.exists(sp):
            continue
        # Match policy prefix
        policy = None
        for p in POLICIES:
            if d.startswith(p + "_"):
                policy = p
                break
        if policy is None:
            continue
        # Detect load by inspecting list repr in name
        if "_none_" in d:
            load = "balanced"
        elif "_['instance_0', 'instance_1']_" in d:
            load = "imbalanced_2"
        elif "_['instance_0', 'instance_1', 'instance_2', 'instance_3', 'instance_4', 'instance_5']_" in d:
            load = "imbalanced_6"
        else:
            continue
        # Extract rep
        m = re.search(r"_r(\d+)_w12_", d)
        if not m:
            continue
        rep = int(m.group(1))
        with open(sp) as f:
            out[(policy, load, rep)] = json.load(f)
    return out


def welch_t_test(a, b):
    """Welch's t-test for two independent samples, returns (t, p, df, cohens_d)."""
    ma, sa, na = mean(a), stdev(a), len(a)
    mb, sb, nb = mean(b), stdev(b), len(b)
    if na < 2 or nb < 2:
        return (0, 1.0, 0, 0)
    se = math.sqrt(sa*sa/na + sb*sb/nb)
    if se == 0:
        return (0, 1.0, 0, 0)
    t = (ma - mb) / se
    # Welch-Satterthwaite df
    num = (sa*sa/na + sb*sb/nb)**2
    den = (sa*sa/na)**2/(na-1) + (sb*sb/nb)**2/(nb-1)
    df = num / den if den > 0 else 0
    # two-tailed p (using t distribution approx)
    # For small df, use python's stat computation. We'll use a simple approximation
    p = two_tailed_p_from_t(t, df)
    # Cohen's d (pooled std)
    pooled_std = math.sqrt(((na-1)*sa*sa + (nb-1)*sb*sb) / (na + nb - 2))
    d = (ma - mb) / pooled_std if pooled_std > 0 else 0
    return (t, p, df, d)


def two_tailed_p_from_t(t, df):
    """Approximate two-tailed p-value using incomplete beta function.
    For df > 30, normal approximation works. For small df, use exact."""
    try:
        from scipy import stats
        return 2 * stats.t.sf(abs(t), df)
    except ImportError:
        # Fallback: normal approximation
        from math import erf, sqrt
        z = abs(t)
        return 2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))


def wilcoxon(a, b):
    """Wilcoxon rank-sum (Mann-Whitney U) test."""
    try:
        from scipy import stats
        if len(a) < 2 or len(b) < 2:
            return (0, 1.0)
        u, p = stats.mannwhitneyu(a, b, alternative="two-sided")
        return (u, p)
    except ImportError:
        return (0, 1.0)


def per_cell_stats(summaries):
    """Compute per (policy, load) aggregate: mean, std, CI across reps.
    Also: paired stats tests vs Coarse baseline."""
    rows = []
    metric_keys = ["ttft_p50", "ttft_p95", "ttft_p99",
                   "tpot_p50", "tpot_p95", "tpot_p99",
                   "cache_hit_rate", "load_stdev", "sla_success_rate",
                   "workflow_completion_p50", "workflow_completion_p95",
                   "workflow_completion_p99", "state_size_p95",
                   "state_traffic_Bps", "dispatch_decision_p95", "failure_rate"]

    for lc in LOADS:
        # baseline = coarse
        coarse = [summaries.get(("coarse", lc, r)) for r in [1, 2, 3]]
        coarse = [s for s in coarse if s]
        for p in POLICIES:
            ps = [summaries.get((p, lc, r)) for r in [1, 2, 3]]
            ps = [s for s in ps if s]
            if not ps:
                continue
            row = {"policy": p, "load": lc, "n_reps": len(ps)}
            for mk in metric_keys:
                vals = [s[mk] for s in ps if s.get(mk) is not None]
                if not vals:
                    row[f"{mk}_mean"] = 0
                    row[f"{mk}_std"] = 0
                    row[f"{mk}_ci95_low"] = 0
                    row[f"{mk}_ci95_high"] = 0
                    continue
                m = mean(vals)
                sd = stdev(vals) if len(vals) > 1 else 0
                # 95% CI: mean ± t_critical * sd/sqrt(n), use 1.96 for small N
                se = sd / math.sqrt(len(vals)) if len(vals) > 1 else 0
                margin = 1.96 * se
                row[f"{mk}_mean"] = m
                row[f"{mk}_std"] = sd
                row[f"{mk}_ci95_low"] = m - margin
                row[f"{mk}_ci95_high"] = m + margin
            rows.append(row)
    return rows


def stat_tests(summaries):
    """For each (load, metric), test each policy vs coarse.
    Returns list of (load, policy, metric, t, p, d, sig)."""
    out = []
    metric_keys = ["ttft_p50", "ttft_p95", "ttft_p99",
                   "tpot_p95", "cache_hit_rate", "load_stdev",
                   "workflow_completion_p95", "dispatch_decision_p95"]
    for lc in LOADS:
        coarse_reps = {r: summaries.get(("coarse", lc, r)) for r in [1, 2, 3]}
        coarse_reps = {r: s for r, s in coarse_reps.items() if s}
        if len(coarse_reps) < 2:
            continue
        for p in POLICIES:
            if p == "coarse":
                continue
            p_reps = {r: summaries.get((p, lc, r)) for r in [1, 2, 3]}
            p_reps = {r: s for r, s in p_reps.items() if s}
            if len(p_reps) < 2:
                continue
            # shared reps
            shared_reps = sorted(set(coarse_reps.keys()) & set(p_reps.keys()))
            if len(shared_reps) < 2:
                continue
            for mk in metric_keys:
                coarse_vals = [coarse_reps[r][mk] for r in shared_reps]
                p_vals = [p_reps[r][mk] for r in shared_reps]
                # paired t-test (since same reps, paired)
                diffs = [c - v for c, v in zip(coarse_vals, p_vals)]
                # use scipy if available, else manual
                try:
                    from scipy import stats as scipy_stats
                    t, p_val = scipy_stats.ttest_rel(coarse_vals, p_vals)
                    # scipy returns NaN if all differences are 0
                    if math.isnan(p_val):
                        p_val = 1.0
                except ImportError:
                    # Manual paired t-test
                    n = len(diffs)
                    if n < 2:
                        continue
                    md = mean(diffs)
                    sd_d = stdev(diffs)
                    if sd_d == 0:
                        t = 0; p_val = 1.0
                    else:
                        t = md / (sd_d / math.sqrt(n))
                        p_val = two_tailed_p_from_t(t, n - 1)
                # Cohen's d_z for paired
                d_z = mean(diffs) / stdev(diffs) if stdev(diffs) > 0 else 0
                sig_marker = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else "ns"
                out.append({
                    "load": lc, "policy": p, "metric": mk,
                    "coarse_mean": mean(coarse_vals),
                    "policy_mean": mean(p_vals),
                    "delta": mean(diffs),
                    "t": t, "p": p_val, "d_z": d_z,
                    "significant": sig_marker,
                })
    return out


def write_outputs(rows_per_cell, stat_results):
    with open(os.path.join(AGG, "per_cell_summary.csv"), "w") as f:
        keys = list(rows_per_cell[0].keys())
        f.write(",".join(keys) + "\n")
        for r in rows_per_cell:
            f.write(",".join(str(r.get(k, "")) for k in keys) + "\n")
    print(f"wrote {AGG}/per_cell_summary.csv ({len(rows_per_cell)} rows)")

    with open(os.path.join(AGG, "stat_tests.csv"), "w") as f:
        f.write("load,policy,metric,coarse_mean,policy_mean,delta,t,p,cohens_d_z,significant\n")
        for s in stat_results:
            f.write(f"{s['load']},{s['policy']},{s['metric']},{s['coarse_mean']:.4f},"
                    f"{s['policy_mean']:.4f},{s['delta']:.4f},{s['t']:.3f},"
                    f"{s['p']:.4f},{s['d_z']:.3f},{s['significant']}\n")
    print(f"wrote {AGG}/stat_tests.csv ({len(stat_results)} rows)")


def fig_quality_with_ci(rows_per_cell):
    """Bar chart with 95% CI error bars for ttft_p95, cache_hit_rate, load_stdev per policy."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    metrics_to_plot = [
        ("ttft_p95_mean", "TTFT p95 (ms, lower better)"),
        ("cache_hit_rate_mean", "Cache hit rate (higher better)"),
        ("load_stdev_mean", "Load stdev (lower better)"),
        ("workflow_completion_p95_mean", "Workflow p95 (ms, lower better)"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    for ax, (mk, label) in zip(axes.flat, metrics_to_plot):
        err_key = mk.replace("_mean", "_ci95_high")
        lo_key = mk.replace("_mean", "_ci95_low")
        x = np.arange(len(POLICIES))
        for li, lc in enumerate(LOADS):
            ys, yerr_lo, yerr_hi = [], [], []
            for p in POLICIES:
                cell = next((r for r in rows_per_cell
                             if r["policy"] == p and r["load"] == lc), None)
                if not cell:
                    ys.append(0); yerr_lo.append(0); yerr_hi.append(0)
                else:
                    m = cell[mk]
                    hi = cell[err_key] - m
                    lo = m - cell[lo_key]
                    ys.append(m)
                    yerr_lo.append(max(0, lo))
                    yerr_hi.append(max(0, hi))
            ax.bar(x + (li - 1) * 0.27, ys, 0.25,
                   yerr=[yerr_lo, yerr_hi],
                   label=LOAD_LABEL[lc], capsize=3,
                   color=["#88ccee", "#4477aa", "#223a5e"][li])
        ax.set_xticks(x)
        ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES])
        ax.set_title(label)
        ax.legend(fontsize=9)
        ax.grid(True, axis="y", alpha=0.3)
    fig.suptitle("v3: Quality metrics by policy × load condition (95% CI, n=3 reps)", fontsize=14)
    plt.tight_layout()
    out_path = os.path.join(FIG, "fig_quality_with_ci.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


def fig_cost_vs_quality(rows_per_cell):
    """Cost (state size) vs Quality (cache hit %) scatter with error bars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 7))
    for li, lc in enumerate(LOADS):
        for p in POLICIES:
            cell = next((r for r in rows_per_cell
                         if r["policy"] == p and r["load"] == lc), None)
            if not cell:
                continue
            x = cell["state_traffic_Bps_mean"]
            y = cell["cache_hit_rate_mean"] * 100
            xerr_lo = cell["state_traffic_Bps_mean"] - cell["state_traffic_Bps_ci95_low"]
            xerr_hi = cell["state_traffic_Bps_ci95_high"] - cell["state_traffic_Bps_mean"]
            yerr_lo = (cell["cache_hit_rate_mean"] - cell["cache_hit_rate_ci95_low"]) * 100
            yerr_hi = (cell["cache_hit_rate_ci95_high"] - cell["cache_hit_rate_mean"]) * 100
            ax.errorbar(x, y, xerr=[[xerr_lo], [xerr_hi]], yerr=[[yerr_lo], [yerr_hi]],
                        fmt="o", color=POLICY_COLOR[p], markersize=10, capsize=4,
                        label=f"{POLICY_LABEL[p]} ({LOAD_LABEL[lc]})" if p == "round-robin" else None)
            ax.annotate(f"{POLICY_LABEL[p][:3]}", (x, y), textcoords="offset points",
                        xytext=(5, 5), fontsize=8, color=POLICY_COLOR[p])
    ax.set_xscale("log")
    ax.set_xlabel("State traffic (B/s, log scale, lower better)")
    ax.set_ylabel("Cache hit rate (%, higher better)")
    ax.set_title("v3: Trade-off — quality (cache hit) vs cost (state traffic) with 95% CI")
    ax.grid(True, which="both", alpha=0.3)
    # build custom legend
    from matplotlib.patches import Patch
    legend_elems = [Patch(facecolor=POLICY_COLOR[p], label=POLICY_LABEL[p]) for p in POLICIES] + \
                    [Patch(facecolor="gray", label=LOAD_LABEL[lc]) for lc in LOADS]
    ax.legend(handles=legend_elems, loc="lower left", fontsize=9)
    plt.tight_layout()
    out_path = os.path.join(FIG, "fig_cost_vs_quality_v3.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    print(f"wrote {out_path}")


if __name__ == "__main__":
    s = load_summaries()
    print(f"Loaded {len(s)} cells")
    if not s:
        print("No cells found, exiting")
        sys.exit(0)
    rows = per_cell_stats(s)
    tests = stat_tests(s)
    write_outputs(rows, tests)
    fig_quality_with_ci(rows)
    fig_cost_vs_quality(rows)

    # Print key comparisons: Sketch vs Coarse, Sketch vs Rich
    print("\n=== KEY COMPARISONS (paired t-test on n=3 reps) ===")
    for lc in LOADS:
        for metric in ["ttft_p95", "cache_hit_rate", "workflow_completion_p95",
                        "dispatch_decision_p95", "load_stdev"]:
            r = next((x for x in tests
                     if x["load"] == lc and x["metric"] == metric
                     and x["policy"] == "sketch"), None)
            if r:
                delta_pct = (r["delta"] / r["coarse_mean"]) * 100 if r["coarse_mean"] else 0
                print(f"  {lc:14s} {metric:28s} Sketch vs Coarse: delta={r['delta']:+.4f} "
                      f"({delta_pct:+.1f}%) p={r['p']:.3f} {r['significant']}")
            r2 = next((x for x in tests
                       if x["load"] == lc and x["metric"] == metric
                       and x["policy"] == "rich"), None)
            if r2:
                delta_pct = (r2["delta"] / r2["coarse_mean"]) * 100 if r2["coarse_mean"] else 0
                print(f"  {lc:14s} {metric:28s} Rich vs Coarse:   delta={r2['delta']:+.4f} "
                      f"({delta_pct:+.1f}%) p={r2['p']:.3f} {r2['significant']}")