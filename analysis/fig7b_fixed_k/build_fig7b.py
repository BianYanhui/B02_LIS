#!/usr/bin/env python3
"""Extract Fig. 7(b) from the frozen 10 Mbit ov30 sweep. Does not start a run."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path("/home/byh/B02")
SRC = ROOT / "analysis/icc_kv/formal_A_5d2d300/ksweep_ov30/e2e_summary.json"
OUT = ROOT / "analysis/fig7b_fixed_k"
WIRE_BYTES = 104
KS = (4, 8, 16, 32, 64)
BOUNDED_KS = {4, 16, 64}


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def load() -> tuple[dict, list[dict]]:
    meta = json.loads(SRC.read_text())
    return meta, meta["rows"]


def cell_row(meta: dict, row: dict) -> dict:
    k = int(str(row["method"]).replace("StaticTopK", ""))
    scored = int(row["scored_requests"])
    forwarded = int(row["forwarded"]) if "forwarded" in row else int(row["relay_forwarded"])
    fn_rate = float(row["loose_false_negative_rate"])
    fn_count = round(fn_rate * scored)
    return {
        "source": "formal_A_5d2d300/ksweep_ov30",
        "git_commit": meta["commit"],
        "workload": "ov30",
        "noise_coverage_mix": row["noise_coverage_mix"],
        "noise_overlap": row["noise_overlap"],
        "useful_pool": row["useful_pool"],
        "useful_coverage_mix": row["useful_coverage_mix"],
        "scenario": row["scenario"],
        "measured_rho": row["measured_rho"],
        "link_bit_s": meta["fixed_link_bit_s"],
        "wire_bytes": WIRE_BYTES,
        "seed": row["seed"],
        "method": row["method"],
        "K": k,
        "routing_fn_rate": fn_rate,
        "routing_fn_pct": fn_rate * 100.0,
        "routing_fn_count": fn_count,
        "scored_requests": scored,
        "ttft_mean_ms": row["ttft_mean_ms"],
        "reused_tokens_mean": row["reused_tokens_mean"],
        "forwarded_frames": forwarded,
        "forwarded_bytes": forwarded * WIRE_BYTES,
        "forwarded_bytes_per_scored_request": forwarded * WIRE_BYTES / scored,
        "forwarded_per_s": row["forwarded_per_s"],
        "relay_drop_global_topk": row["relay_drop_global_topk"],
        "relay_drop_superseded": row["relay_drop_superseded"],
        "relay_drop_replica_cap": row["relay_drop_replica_cap"],
        "relay_drop_low_utility": row["relay_drop_low_utility"],
        "relay_drop_queue_drop": row["relay_drop_queue_drop"],
        "foreground_undelivered": row["foreground_undelivered"],
        "foreground_applied": row["foreground_applied"],
        "foreground_up_sent": row["foreground_up_sent"],
        "fifo_peak": row["relay_max_queue"],
        "routable_view_max": row["routable_view_max"],
        "pending_lineage_mean": "NOT_MEASURED",
        "pending_lineage_p95": "NOT_MEASURED",
        "congested_fraction": row["congested_fraction"],
        "failed_requests": row["failed_requests"],
        "ledger_balance_gap": row["ledger_balance_gap"],
        "label": "OBSERVED",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for k in KS:
        items = [row for row in rows if row["K"] == k]
        def vals(key: str) -> list[float]:
            return [float(row[key]) for row in items]
        fn = vals("routing_fn_pct")
        ttft = vals("ttft_mean_ms")
        reuse = vals("reused_tokens_mean")
        cost = vals("forwarded_bytes_per_scored_request")
        fwd = vals("forwarded_per_s")
        drops = vals("relay_drop_global_topk")
        peak = vals("fifo_peak")
        undel = vals("foreground_undelivered")
        deltas = [row["delta_fn_pct_vs_bounded_semantic"] for row in items]
        delta_known = [float(item) for item in deltas if item != "NOT_MEASURED"]
        out.append({
            "K": k,
            "n_seeds": len(items),
            "routing_fn_pct_mean": mean(fn),
            "routing_fn_pct_min": min(fn),
            "routing_fn_pct_max": max(fn),
            "routing_fn_count_sum": sum(int(row["routing_fn_count"]) for row in items),
            "scored_requests_per_seed": items[0]["scored_requests"],
            "ttft_mean_ms_mean": mean(ttft),
            "ttft_mean_ms_min": min(ttft),
            "ttft_mean_ms_max": max(ttft),
            "reused_tokens_mean_mean": mean(reuse),
            "reused_tokens_mean_min": min(reuse),
            "reused_tokens_mean_max": max(reuse),
            "forwarded_bytes_per_request_mean": mean(cost),
            "forwarded_bytes_per_request_min": min(cost),
            "forwarded_bytes_per_request_max": max(cost),
            "forwarded_per_s_mean": mean(fwd),
            "forwarded_per_s_min": min(fwd),
            "forwarded_per_s_max": max(fwd),
            "topk_drops_mean": mean(drops),
            "topk_drops_min": min(drops),
            "topk_drops_max": max(drops),
            "fifo_peak_mean": mean(peak),
            "fifo_peak_min": min(peak),
            "fifo_peak_max": max(peak),
            "foreground_undelivered_mean": mean(undel),
            "delta_fn_pct_vs_bounded_semantic_mean": mean(delta_known) if delta_known else "NOT_MEASURED",
            "delta_fn_pct_vs_bounded_semantic_min": min(delta_known) if delta_known else "NOT_MEASURED",
            "delta_fn_pct_vs_bounded_semantic_max": max(delta_known) if delta_known else "NOT_MEASURED",
            "pending_lineage_mean": "NOT_MEASURED",
            "pending_lineage_p95": "NOT_MEASURED",
            "admission_cap_binding": "BINDING" if min(drops) > 0 else "NOT_BINDING",
            "errorbar": "seed_min_max",
        })
    return out


def configure() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
        "font.size": 7.2,
        "axes.labelsize": 7.2,
        "axes.titlesize": 7.5,
        "legend.fontsize": 6.1,
        "xtick.labelsize": 6.5,
        "ytick.labelsize": 6.5,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "axes.linewidth": 0.65,
        "lines.linewidth": 1.1,
        "lines.markersize": 4.0,
    })


def plot_fn(summary: list[dict], path: Path) -> None:
    configure()
    xs = [row["K"] for row in summary]
    ys = [row["routing_fn_pct_mean"] for row in summary]
    yerr = [
        [ys[i] - row["routing_fn_pct_min"] for i, row in enumerate(summary)],
        [row["routing_fn_pct_max"] - ys[i] for i, row in enumerate(summary)],
    ]
    fig, ax = plt.subplots(figsize=(3.5, 2.55))
    ax.errorbar(
        xs, ys, yerr=yerr, fmt="-o", color="#1f4e79", ecolor="#1f4e79",
        elinewidth=0.8, capsize=2.2, capthick=0.7, label="Static KVSelect",
    )
    default = next(row for row in summary if row["K"] == 16)
    ax.plot(
        [16], [default["routing_fn_pct_mean"]], marker="D", markersize=5.2,
        color="#b35c1e", linestyle="None", label="K = 16 default", zorder=4,
    )
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(k) for k in xs])
    ax.set_xlim(3.2, 80)
    ax.set_ylim(0, max(row["routing_fn_pct_max"] for row in summary) * 1.25)
    ax.set_xlabel("Pending lineage capacity K")
    ax.set_ylabel("Routing FN (%)")
    ax.set_title("Fixed-K Sensitivity")
    ax.grid(True, axis="y", linewidth=0.4, alpha=0.5)
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index("Static KVSelect"), labels.index("K = 16 default")]
    ax.legend([handles[i] for i in order], [labels[i] for i in order], frameon=False, loc="upper right")
    fig.tight_layout(pad=0.35)
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)


def plot_cost(summary: list[dict], path: Path) -> None:
    configure()
    xs = [row["K"] for row in summary]
    ys = [row["forwarded_bytes_per_request_mean"] / 1e6 for row in summary]
    yerr = [
        [(row["forwarded_bytes_per_request_mean"] - row["forwarded_bytes_per_request_min"]) / 1e6 for row in summary],
        [(row["forwarded_bytes_per_request_max"] - row["forwarded_bytes_per_request_mean"]) / 1e6 for row in summary],
    ]
    fig, ax = plt.subplots(figsize=(3.5, 2.55))
    ax.errorbar(
        xs, ys, yerr=yerr, fmt="-s", color="#1f4e79", ecolor="#1f4e79",
        elinewidth=0.8, capsize=2.2, capthick=0.7, label="Static KVSelect",
    )
    ax.plot([16], [next(row["forwarded_bytes_per_request_mean"] for row in summary if row["K"] == 16) / 1e6],
            marker="D", markersize=5.2, color="#b35c1e", linestyle="None", label="K = 16 default", zorder=4)
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(k) for k in xs])
    ax.set_xlim(3.2, 80)
    ax.set_xlabel("Pending lineage capacity K")
    ax.set_ylabel("Forwarded metadata (MB/request)")
    ax.set_title("Signaling cost")
    ax.grid(True, axis="y", linewidth=0.4, alpha=0.5)
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index("Static KVSelect"), labels.index("K = 16 default")]
    ax.legend([handles[i] for i in order], [labels[i] for i in order], frameon=False, loc="upper left")
    fig.tight_layout(pad=0.35)
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)


def main() -> None:
    meta, raw = load()
    if meta.get("frame_bytes") != 64 or meta.get("wire_bytes") != WIRE_BYTES:
        raise SystemExit(f"unexpected frame size in {SRC}")
    static = [cell_row(meta, row) for row in raw if str(row["method"]).startswith("StaticTopK")]
    bounded = {
        (int(str(row["method"]).replace("BoundedSemantic", "")), int(row["seed"])): float(row["loose_false_negative_rate"])
        for row in raw if str(row["method"]).startswith("BoundedSemantic")
    }
    for row in static:
        key = (row["K"], int(row["seed"]))
        if row["K"] not in BOUNDED_KS:
            row["bounded_semantic_fn_pct"] = "NOT_MEASURED"
            row["delta_fn_pct_vs_bounded_semantic"] = "NOT_MEASURED"
        else:
            other = bounded[key] * 100.0
            row["bounded_semantic_fn_pct"] = other
            row["delta_fn_pct_vs_bounded_semantic"] = row["routing_fn_pct"] - other
    static.sort(key=lambda row: (row["K"], row["seed"]))
    summary = summarize(static)
    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "fig7b_k_sensitivity.csv", static)
    write_csv(OUT / "fig7b_k_sensitivity_summary.csv", summary)
    plot_fn(summary, OUT / "fig7b_k_sensitivity.pdf")
    plot_cost(summary, OUT / "fig7b_signaling_cost.pdf")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
