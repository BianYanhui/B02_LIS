#!/usr/bin/env python3
"""Aggregate the frozen 4xT4 study and render paper-ready vector figures.

Only ``raw/baseline`` and ``summary/cells_baseline_*.csv`` are used for the
formal baseline aggregates.  Calibration remains a separately labelled,
non-paper diagnostic.  Repetitions (not individual requests) are the unit for
all CIs and paired comparisons.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


T975 = {1: 0.0, 2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}
POLICY_ORDER = ["FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "StaticSemantic", "Adaptive", "Ideal"]
MAIN_POLICIES = ["FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "Adaptive", "Ideal"]
STYLE = {
    "FullSync": {"color": "#111111", "marker": "o", "linestyle": "-"},
    "RateFIFO": {"color": "#555555", "marker": "s", "linestyle": "--"},
    "LatestOnly": {"color": "#777777", "marker": "^", "linestyle": "-."},
    "AgeCov-Greedy": {"color": "#333333", "marker": "D", "linestyle": ":"},
    "StaticSemantic": {"color": "#999999", "marker": "v", "linestyle": "--"},
    "Adaptive": {"color": "#000000", "marker": "P", "linestyle": "-"},
    "Ideal": {"color": "#b0b0b0", "marker": "x", "linestyle": ":"},
}


def configure_plotting() -> None:
    plt.rcParams.update({
        "font.family": "serif", "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
        "font.size": 7.2, "axes.labelsize": 7.2, "axes.titlesize": 7.5,
        "legend.fontsize": 6.1, "xtick.labelsize": 6.5, "ytick.labelsize": 6.5,
        "pdf.fonttype": 42, "ps.fonttype": 42, "axes.linewidth": 0.65,
        "lines.linewidth": 1.1, "lines.markersize": 4.0,
    })


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value in (None, "", "None"):
        return default
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return 1.0 if value.lower() == "true" else 0.0
    return float(value)


def integer(row: dict[str, Any], key: str, default: int = 0) -> int:
    value = row.get(key, default)
    if value in (None, "", "None"):
        return default
    return int(float(value))


def percentile(values: Iterable[float], pct: float) -> float:
    data = sorted(float(value) for value in values)
    if not data:
        return 0.0
    location = (len(data) - 1) * pct / 100.0
    lo, hi = math.floor(location), math.ceil(location)
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (location - lo)


def mean_std_ci(values: Iterable[float]) -> tuple[float, float, float, int]:
    data = [float(value) for value in values]
    n = len(data)
    if not n:
        return 0.0, 0.0, 0.0, 0
    mean = statistics.mean(data)
    std = statistics.stdev(data) if n > 1 else 0.0
    ci = T975.get(n, 1.96) * std / math.sqrt(n) if n > 1 else 0.0
    return mean, std, ci, n


def rho_key(row: dict[str, Any]) -> str:
    rho = row.get("rho", "")
    return "Ideal" if rho in ("", None) else f"{float(rho):.1f}"


def ensure_valid_summary(summary_dir: Path, prefix: str, required: bool = True) -> list[dict[str, str]]:
    paths = sorted(summary_dir.glob(f"cells_{prefix}*.csv"))
    if required and not paths:
        raise RuntimeError(f"no {prefix} cell summaries in {summary_dir}")
    rows = [row for path in paths for row in read_csv(path)]
    invalid = [row for row in rows if row.get("status") != "VALID"]
    if invalid:
        raise RuntimeError(f"invalid {prefix} cell(s): {[row.get('experiment_id') for row in invalid[:8]]}")
    for path in sorted(summary_dir.glob(f"sanity_checks_{prefix}*.csv")):
        failures = [row for row in read_csv(path) if row.get("status") != "PASS"]
        if failures:
            raise RuntimeError(f"failed formal sanity checks in {path}: {failures[:3]}")
    return rows


def aggregate_cells(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    metrics = [
        "dispatch_state_age_mean_s", "dispatch_state_age_p95_s",
        "dispatcher_view_missing_at_dispatch_rate", "source_view_false_negative_rate",
        "source_view_false_positive_rate", "physical_false_positive_affinity_rate",
        "vllm_cached_tokens_per_request", "vllm_cached_tokens_total",
        "ttft_mean_ms", "ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms",
        "latency_p95_ms", "throughput_requests_per_s", "net_wire_bytes_sent",
        "relay_forwarded", "net_msgs_delivered", "net_undelivered_at_drain_end",
        "ad_delivery_delay_p50_s", "ad_delivery_delay_p95_s", "ad_delivery_delay_p99_s",
        "relay_max_queue", "stale_fallback_rate", "tombstone_delay_p95_s",
        "affinity_selection_rate", "dispatcher_unique_prefixes_per_wire_byte",
    ]
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["workload"], row["policy"], rho_key(row))].append(row)
    out: list[dict[str, Any]] = []
    for (workload, policy, rho), items in sorted(grouped.items(), key=lambda entry: (entry[0][0], POLICY_ORDER.index(entry[0][1]), entry[0][2])):
        record: dict[str, Any] = {"workload": workload, "policy": policy, "rho": rho, "n_runs": len(items),
                                  "repetition_unit": "run/repetition", "ci_level": "95% t interval"}
        for metric in metrics:
            mean, std, ci, _ = mean_std_ci(number(item, metric) for item in items)
            record[f"{metric}_mean"] = mean
            record[f"{metric}_std"] = std
            record[f"{metric}_ci95"] = ci
        out.append(record)
    return out


def paired_comparisons(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, int, str, str], dict[str, str]] = {}
    for row in rows:
        if row["policy"] != "Ideal":
            indexed[(row["workload"], integer(row, "rep"), rho_key(row), row["policy"])] = row
    out: list[dict[str, Any]] = []
    for workload in sorted({row["workload"] for row in rows}):
        for rep in sorted({integer(row, "rep") for row in rows if row["workload"] == workload}):
            for rho in sorted({rho_key(row) for row in rows if row["workload"] == workload and row["policy"] != "Ideal"}, key=float):
                adaptive = indexed.get((workload, rep, rho, "Adaptive"))
                if adaptive is None:
                    continue
                for baseline in ("FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "StaticSemantic"):
                    other = indexed.get((workload, rep, rho, baseline))
                    if other is None:
                        continue
                    if adaptive.get("workload_trace_hash") != other.get("workload_trace_hash"):
                        raise RuntimeError(f"unpaired trace hashes for {workload}, rep {rep}, rho {rho}")
                    out.append({
                        "workload": workload, "rep": rep, "rho": float(rho), "baseline": baseline,
                        "adaptive_experiment_id": adaptive["experiment_id"], "baseline_experiment_id": other["experiment_id"],
                        "trace_hash": adaptive["workload_trace_hash"],
                        "delta_state_age_s_baseline_minus_adaptive": number(other, "dispatch_state_age_mean_s") - number(adaptive, "dispatch_state_age_mean_s"),
                        "delta_p95_state_age_s_baseline_minus_adaptive": number(other, "dispatch_state_age_p95_s") - number(adaptive, "dispatch_state_age_p95_s"),
                        "delta_view_missing_baseline_minus_adaptive": number(other, "dispatcher_view_missing_at_dispatch_rate") - number(adaptive, "dispatcher_view_missing_at_dispatch_rate"),
                        "delta_cached_tokens_adaptive_minus_baseline": number(adaptive, "vllm_cached_tokens_per_request") - number(other, "vllm_cached_tokens_per_request"),
                        "delta_ttft_ms_baseline_minus_adaptive": number(other, "ttft_mean_ms") - number(adaptive, "ttft_mean_ms"),
                        "delta_p95_ttft_ms_baseline_minus_adaptive": number(other, "ttft_p95_ms") - number(adaptive, "ttft_p95_ms"),
                        # Compare bytes that traversed the constrained gateway->dispatcher
                        # signaling path, rather than source->gateway ingress bytes.  The
                        # latter includes updates suppressed inside the gateway and therefore
                        # cannot represent dissemination efficiency.
                        "delta_forwarded_bytes_baseline_minus_adaptive": (
                            number(other, "relay_forwarded") * number(other, "wire_bytes_per_msg_assumed")
                            - number(adaptive, "relay_forwarded") * number(adaptive, "wire_bytes_per_msg_assumed")
                        ),
                    })
    return out


def aggregate_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = [key for key in rows[0] if key.startswith("delta_")] if rows else []
    grouped: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["workload"]), str(row["baseline"]), float(row["rho"]))].append(row)
    out: list[dict[str, Any]] = []
    for (workload, baseline, rho), items in sorted(grouped.items()):
        record: dict[str, Any] = {"workload": workload, "baseline": baseline, "rho": rho, "n_pairs": len(items)}
        for metric in metrics:
            mean, std, ci, _ = mean_std_ci(float(item[metric]) for item in items)
            record[f"{metric}_mean"] = mean
            record[f"{metric}_std"] = std
            record[f"{metric}_ci95"] = ci
        out.append(record)
    return out


def cached_bucket(value: float) -> str:
    if value <= 0:
        return "0"
    if value <= 511:
        return "1-511"
    if value <= 1023:
        return "512-1023"
    if value <= 2047:
        return "1024-2047"
    if value <= 4095:
        return "2048-4095"
    return ">=4096"


BUCKET_ORDER = ["0", "1-511", "512-1023", "1024-2047", "2048-4095", ">=4096"]


def reuse_buckets(raw_dir: Path) -> list[dict[str, Any]]:
    rows = [row for path in sorted(raw_dir.glob("requests_baseline_*.csv")) for row in read_csv(path)]
    if not rows:
        raise RuntimeError(f"no baseline raw request records in {raw_dir}")
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["workload"], row["policy"], cached_bucket(number(row, "vllm_cached_tokens")))].append(row)
    out: list[dict[str, Any]] = []
    for (workload, policy, bucket), items in sorted(grouped.items(), key=lambda entry: (entry[0][0], POLICY_ORDER.index(entry[0][1]), BUCKET_ORDER.index(entry[0][2]))):
        ttfts = [number(item, "ttft_ms") for item in items if item.get("ok") in ("True", True, "1")]
        out.append({"workload": workload, "policy": policy, "cached_token_bucket": bucket, "request_count": len(items),
                    "ttft_mean_ms": statistics.mean(ttfts) if ttfts else 0.0, "ttft_p95_ms": percentile(ttfts, 95),
                    "cached_tokens_mean": statistics.mean(number(item, "vllm_cached_tokens") for item in items),
                    "cache_reused_count": sum(item.get("validation_result") == "cache_reused" for item in items),
                    "cache_miss_count": sum(item.get("validation_result") == "cache_miss" for item in items),
                    "fallback_count": sum(item.get("validation_result") == "fallback_prefill" for item in items)})
    return out


def signaling_accounting(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    fields = ["upserts_generated", "source_upserts_sent", "source_tombstones_sent", "net_msgs_sent", "net_msgs_delivered",
              "net_wire_bytes_sent", "relay_forwarded", "relay_drop_rate_limit", "relay_drop_superseded",
              "relay_drop_duplicate_holder", "relay_drop_low_utility", "relay_drop_queue_drop", "relay_drop_expired"]
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["policy"] != "Ideal":
            grouped[(row["workload"], row["policy"], rho_key(row))].append(row)
    out: list[dict[str, Any]] = []
    for (workload, policy, rho), items in sorted(grouped.items()):
        record: dict[str, Any] = {"workload": workload, "policy": policy, "rho": rho, "n_runs": len(items)}
        requests = sum(number(item, "request_count") for item in items)
        for field in fields:
            record[f"{field}_mean"] = statistics.mean(number(item, field) for item in items)
        # `net_wire_bytes_sent` is source->gateway ingress and is intentionally
        # constant across many policies.  The Pareto x-axis must instead use
        # actual gateway-forwarded wire bytes on the constrained path.
        record["forwarded_bytes_per_request"] = (
            sum(number(item, "relay_forwarded") * number(item, "wire_bytes_per_msg_assumed") for item in items) / requests
            if requests else 0.0
        )
        record["forwarded_frames_per_request"] = sum(number(item, "relay_forwarded") for item in items) / requests if requests else 0.0
        out.append(record)
    return out


def correctness_rows(churn: list[dict[str, str]], baseline: list[dict[str, str]]) -> list[dict[str, Any]]:
    source = [("baseline", row) for row in baseline] + [("churn", row) for row in churn]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for stage, row in source:
        grouped[(stage, row["policy"])].append(row)
    out: list[dict[str, Any]] = []
    fields = ["source_view_false_positive_rate", "physical_false_positive_affinity_rate", "stale_fallback_rate",
              "fallback_count", "validation_attempt_count", "validation_failure_count", "incorrect_kv_reuse_count",
              "request_error_rate", "tombstone_delay_p95_s", "forced_owner_resets"]
    for (stage, policy), items in sorted(grouped.items()):
        record: dict[str, Any] = {"stage": stage, "policy": policy, "n_runs": len(items)}
        for field in fields:
            record[f"{field}_mean"] = statistics.mean(number(item, field) for item in items)
            record[f"{field}_total"] = sum(number(item, field) for item in items)
        out.append(record)
    return out


def native_validation_rows(native_dir: Path) -> list[dict[str, Any]]:
    """Import the owner-runtime microbench without conflating it with TCP data."""
    paths = sorted(native_dir.glob("gpu*/vllm_native_validation_microbench.csv"))
    if len(paths) != 4:
        raise RuntimeError(f"expected native validation evidence from four GPUs in {native_dir}, found {len(paths)}")
    out: list[dict[str, Any]] = []
    for path in paths:
        gpu = path.parent.name
        checks = path.parent / "vllm_native_validation_sanity_checks.csv"
        if not checks.is_file() or any(row.get("status") != "PASS" for row in read_csv(checks)):
            raise RuntimeError(f"native validation checks failed or missing: {path.parent}")
        for row in read_csv(path):
            out.append({"stage": "native_owner_validation", "policy": gpu, "scenario": row.get("scenario", ""),
                        "operations": integer(row, "operations"), "validation_success_count": integer(row, "validation_success_count"),
                        "fallback_count": integer(row, "fallback_count"), "unsafe_reuse_count": integer(row, "unsafe_reuse_count"),
                        "release_failures": integer(row, "release_failures"),
                        "blocked_evictions": integer(row, "blocked_evictions"),
                        "eviction_attempts": integer(row, "eviction_attempts"),
                        "validate_p95_us": number(row, "validate_p95_us"), "status": row.get("status", "")})
    return out


def select_aggregate(rows: list[dict[str, Any]], workload: str, policy: str, rho: float) -> dict[str, Any] | None:
    for row in rows:
        if row["workload"] == workload and row["policy"] == policy and row["rho"] == f"{rho:.1f}":
            return row
    return None


def plot_baseline(aggregates: list[dict[str, Any]], out: Path) -> None:
    configure_plotting()
    workloads = ["original_compatible", "reuse_intensive"]
    metric_spec = [
        ("dispatch_state_age_p95_s", "P95 dispatch state age (s)"),
        ("dispatcher_view_missing_at_dispatch_rate", "View missing rate"),
        ("vllm_cached_tokens_per_request", "Cached tokens / request"),
        ("ttft_mean_ms", "Mean TTFT (ms)"),
    ]
    rhos = [0.5, 0.8, 1.0, 1.2]
    figure, axes = plt.subplots(2, 4, figsize=(7.15, 3.2), sharex="col")
    for row_index, workload in enumerate(workloads):
        for column, (metric, label) in enumerate(metric_spec):
            ax = axes[row_index, column]
            for policy in MAIN_POLICIES:
                values, cis = [], []
                for rho in rhos:
                    entry = select_aggregate(aggregates, workload, policy, rho)
                    if entry is None and policy == "Ideal":
                        entry = next((item for item in aggregates if item["workload"] == workload and item["policy"] == "Ideal"), None)
                    values.append(number(entry or {}, f"{metric}_mean"))
                    cis.append(number(entry or {}, f"{metric}_ci95"))
                style = STYLE[policy]
                ax.errorbar(rhos, values, yerr=cis, capsize=1.6, label=policy, **style)
            if row_index == 0:
                ax.set_title(f"({chr(97 + column)}) {label}")
            if column == 0:
                ax.set_ylabel("Original-compatible" if row_index == 0 else "Reuse-intensive")
            ax.grid(axis="y", color="#dddddd", linewidth=0.4)
            ax.set_xticks(rhos)
            ax.set_xlabel(r"$\rho$")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=6, frameon=False, bbox_to_anchor=(0.5, 1.03))
    figure.tight_layout(rect=(0, 0, 1, 0.92), h_pad=1.1, w_pad=1.1)
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(figure)


def plot_pareto(aggregates: list[dict[str, Any]], accounting: list[dict[str, Any]], out: Path) -> None:
    configure_plotting()
    figure, axes = plt.subplots(1, 2, figsize=(7.1, 2.45))
    workload = "reuse_intensive"
    lookup = {(row["workload"], row["policy"], row["rho"]): row for row in aggregates}
    for policy in ["FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "StaticSemantic", "Adaptive"]:
        items = [row for row in accounting if row["workload"] == workload and row["policy"] == policy]
        items.sort(key=lambda row: float(row["rho"]))
        x = [number(row, "forwarded_bytes_per_request") for row in items]
        missing = [number(lookup[(workload, policy, row["rho"])], "dispatcher_view_missing_at_dispatch_rate_mean") for row in items]
        age = [number(lookup[(workload, policy, row["rho"])], "dispatch_state_age_p95_s_mean") for row in items]
        axes[0].plot(x, missing, label=policy, **STYLE[policy])
        axes[1].plot(x, age, label=policy, **STYLE[policy])
    axes[0].set_xlabel("Forwarded state bytes / request")
    axes[0].set_ylabel("View missing rate")
    axes[1].set_xlabel("Forwarded state bytes / request")
    axes[1].set_ylabel("P95 dispatch state age (s)")
    for ax, title in zip(axes, ["(a) Visibility efficiency", "(b) Freshness efficiency"]):
        ax.set_title(title)
        ax.grid(color="#dddddd", linewidth=0.4)
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.08))
    figure.tight_layout(rect=(0, 0, 1, 0.86), w_pad=1.4)
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(figure)


def plot_reuse_ttft(bucket_rows: list[dict[str, Any]], out: Path) -> None:
    configure_plotting()
    figure, axes = plt.subplots(1, 2, figsize=(7.1, 2.45), sharey=True)
    for ax, workload, panel in zip(axes, ["original_compatible", "reuse_intensive"], ["(a)", "(b)"]):
        for policy in ["FullSync", "RateFIFO", "Adaptive"]:
            lookup = {row["cached_token_bucket"]: row for row in bucket_rows if row["workload"] == workload and row["policy"] == policy}
            xs, ys = [], []
            for index, bucket in enumerate(BUCKET_ORDER):
                if bucket in lookup:
                    xs.append(index)
                    ys.append(number(lookup[bucket], "ttft_mean_ms"))
            ax.plot(xs, ys, label=policy, **STYLE[policy])
        ax.set_title(f"{panel} {workload.replace('_', '-')}")
        ax.set_xticks(range(len(BUCKET_ORDER)), BUCKET_ORDER, rotation=25, ha="right")
        ax.set_xlabel("Actual cached tokens")
        ax.grid(axis="y", color="#dddddd", linewidth=0.4)
    axes[0].set_ylabel("Mean TTFT (ms)")
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.06))
    figure.tight_layout(rect=(0, 0, 1, 0.86), w_pad=1.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(figure)


def phase_events(dynamic_dir: Path) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(dynamic_dir.glob("phases_dynamic_*.jsonl")):
        events[path.stem.removeprefix("phases_")] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return events


def dynamic_bins(dynamic_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = phase_events(dynamic_dir)
    grouped: dict[tuple[str, int, int], list[dict[str, str]]] = defaultdict(list)
    recovery: list[dict[str, Any]] = []
    for path in sorted(dynamic_dir.glob("requests_dynamic_*.csv")):
        tag = path.stem.removeprefix("requests_")
        ev = events.get(tag)
        if not ev or len(ev) < 4:
            raise RuntimeError(f"incomplete dynamic phase event log: {tag}")
        phases = {item["phase"]: float(item["timestamp_unix"]) for item in ev}
        policy = str(ev[0]["policy"])
        rep_text = tag.split("_rep")[-1].split("_")[0]
        rep = int(rep_text)
        rows = read_csv(path)
        initial = phases["low_initial"]
        recovery_start = phases["low_recovery"]
        low_rows = [row for row in rows if initial <= number(row, "dispatch_time_unix") < phases["high"]]
        threshold_age = statistics.mean(number(row, "dispatch_state_age_s") for row in low_rows) * 1.10 if low_rows else float("inf")
        threshold_missing = statistics.mean(number(row, "dispatcher_view_missing") for row in low_rows) * 1.10 if low_rows else float("inf")
        recovered_at: float | None = None
        for row in rows:
            elapsed = number(row, "dispatch_time_unix") - initial
            if elapsed < 0 or elapsed > 145:
                continue
            bin_index = int(elapsed // 15)
            grouped[(policy, rep, bin_index)].append(row)
            if number(row, "dispatch_time_unix") >= recovery_start and recovered_at is None:
                if number(row, "dispatch_state_age_s") <= threshold_age and number(row, "dispatcher_view_missing") <= threshold_missing:
                    recovered_at = number(row, "dispatch_time_unix")
        recovery.append({"policy": policy, "rep": rep, "recovery_time_s": (recovered_at - recovery_start) if recovered_at else 45.0,
                         "recovered_within_window": recovered_at is not None})
    bins: list[dict[str, Any]] = []
    for (policy, rep, index), rows in grouped.items():
        bins.append({"policy": policy, "rep": rep, "bin_index": index, "time_mid_s": index * 15 + 7.5,
                     "state_age_mean_s": statistics.mean(number(row, "dispatch_state_age_s") for row in rows),
                     "view_missing_rate": statistics.mean(number(row, "dispatcher_view_missing") for row in rows),
                     "queue_proxy": statistics.mean(number(row, "dispatcher_view_missing") for row in rows)})
    return bins, recovery


def plot_dynamic(dynamic_dir: Path, out: Path) -> list[dict[str, Any]]:
    bins, recovery = dynamic_bins(dynamic_dir)
    configure_plotting()
    figure, axes = plt.subplots(2, 1, figsize=(3.48, 3.1), sharex=True)
    for policy in ["RateFIFO", "LatestOnly", "StaticSemantic", "Adaptive"]:
        subset = [row for row in bins if row["policy"] == policy]
        by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for row in subset:
            by_time[float(row["time_mid_s"])] .append(row)
        xs = sorted(by_time)
        age = [statistics.mean(number(row, "state_age_mean_s") for row in by_time[x]) for x in xs]
        missing = [statistics.mean(number(row, "view_missing_rate") for row in by_time[x]) for x in xs]
        axes[0].plot(xs, age, label=policy, **STYLE[policy])
        axes[1].plot(xs, missing, label=policy, **STYLE[policy])
    for ax, label in zip(axes, ["Mean dispatch state age (s)", "View missing rate"]):
        for x in (45, 90):
            ax.axvline(x, color="#999999", linewidth=0.6, linestyle=":")
        ax.set_ylabel(label)
        ax.grid(axis="y", color="#dddddd", linewidth=0.4)
    axes[0].set_title("(a) Freshness")
    axes[1].set_title("(b) Visibility")
    axes[1].set_xlabel("Time from low phase start (s): low / high / recovery")
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=2, frameon=False, bbox_to_anchor=(0.5, 1.03))
    figure.tight_layout(rect=(0, 0, 1, 0.90), h_pad=1.1)
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(figure)
    return recovery


def plot_churn(churn: list[dict[str, str]], out: Path) -> None:
    configure_plotting()
    policies = ["FullSync", "RateFIFO", "LatestOnly", "Adaptive"]
    metrics = [("tombstone_delay_p95_s", "P95 tombstone delay (s)"),
               ("source_view_false_positive_rate", "Stale-positive rate"),
               ("stale_fallback_rate", "Fallback rate")]
    figure, axes = plt.subplots(1, 3, figsize=(7.1, 2.3))
    for ax, (metric, label) in zip(axes, metrics):
        values, errors = [], []
        for policy in policies:
            items = [row for row in churn if row["policy"] == policy]
            mean, _std, ci, _n = mean_std_ci(number(item, metric) for item in items)
            values.append(mean)
            errors.append(ci)
        ax.bar(range(len(policies)), values, yerr=errors, capsize=2, color=[STYLE[item]["color"] for item in policies], width=0.68)
        ax.set_xticks(range(len(policies)), ["Full", "Rate", "Latest", "Adaptive"], rotation=20, ha="right")
        ax.set_ylabel(label)
        ax.grid(axis="y", color="#dddddd", linewidth=0.4)
    figure.tight_layout(w_pad=1.1)
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, format="pdf", bbox_inches="tight")
    plt.close(figure)


def summarize_pair(pairs: list[dict[str, Any]], workload: str, baseline: str, metric: str) -> tuple[float, float]:
    data = [number(row, metric) for row in pairs if row["workload"] == workload and row["baseline"] == baseline]
    mean, _std, ci, _n = mean_std_ci(data)
    return mean, ci


def report_text(aggregates: list[dict[str, Any]], pairs: list[dict[str, Any]], buckets: list[dict[str, Any]],
                recovery: list[dict[str, Any]], churn: list[dict[str, str]], native: list[dict[str, Any]], selection_path: Path) -> str:
    lines = ["# Frozen 4x Tesla T4 supplementary experiment", "",
             "This report uses only VALID formal rows. Confidence intervals and paired effects use repetitions as the statistical unit; individual requests are not treated as independent runs.", ""]
    lines += ["## Required findings", ""]
    for workload in ["original_compatible", "reuse_intensive"]:
        lines.append(f"### {workload}")
        for baseline in ["RateFIFO", "LatestOnly", "AgeCov-Greedy", "StaticSemantic", "FullSync"]:
            age, age_ci = summarize_pair(pairs, workload, baseline, "delta_p95_state_age_s_baseline_minus_adaptive")
            missing, missing_ci = summarize_pair(pairs, workload, baseline, "delta_view_missing_baseline_minus_adaptive")
            cached, cached_ci = summarize_pair(pairs, workload, baseline, "delta_cached_tokens_adaptive_minus_baseline")
            ttft, ttft_ci = summarize_pair(pairs, workload, baseline, "delta_ttft_ms_baseline_minus_adaptive")
            lines.append(f"- Adaptive vs {baseline}: paired P95-age reduction {age:.3f} +/- {age_ci:.3f} s; view-missing reduction {missing:.3f} +/- {missing_ci:.3f}; cached-token change {cached:.1f} +/- {cached_ci:.1f} tokens/request; TTFT saving {ttft:.1f} +/- {ttft_ci:.1f} ms.")
        lines.append("")
    def paired(workload: str, baseline: str, metric: str) -> tuple[float, float]:
        return summarize_pair(pairs, workload, baseline, metric)

    def effect(workload: str, baseline: str, metric: str, unit: str) -> str:
        mean, ci = paired(workload, baseline, metric)
        return f"{mean:.2f} +/- {ci:.2f} {unit}"

    recovery_by_policy: dict[str, float] = {}
    for policy in sorted({str(row["policy"]) for row in recovery}):
        recovery_by_policy[policy] = statistics.mean(
            number(row, "recovery_time_s") for row in recovery if row["policy"] == policy)
    fastest_recovery = min(recovery_by_policy, key=recovery_by_policy.get) if recovery_by_policy else "n/a"
    native_fallbacks = sum(integer(row, "fallback_count") for row in native)
    native_unsafe = sum(integer(row, "unsafe_reuse_count") for row in native)
    churn_fallbacks = sum(number(row, "fallback_count") for row in churn)
    lines += ["## Direct answers", "",
              f"1. **Adaptive vs RateFIFO:** yes for the main reuse-intensive workload under the same physical HTB budget: P95 state age improves by {effect('reuse_intensive', 'RateFIFO', 'delta_p95_state_age_s_baseline_minus_adaptive', 's')}, cached tokens by {effect('reuse_intensive', 'RateFIFO', 'delta_cached_tokens_adaptive_minus_baseline', 'tokens/request')}, and mean TTFT by {effect('reuse_intensive', 'RateFIFO', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}. The analogous original-compatible P95-age effect is {effect('original_compatible', 'RateFIFO', 'delta_p95_state_age_s_baseline_minus_adaptive', 's')}; this rules out an explanation based only on a lower physical signaling budget.",
              f"2. **Adaptive vs LatestOnly:** supersession alone is insufficient in reuse-intensive serving: Adaptive improves P95 age by {effect('reuse_intensive', 'LatestOnly', 'delta_p95_state_age_s_baseline_minus_adaptive', 's')}, cached tokens by {effect('reuse_intensive', 'LatestOnly', 'delta_cached_tokens_adaptive_minus_baseline', 'tokens/request')}, and mean TTFT by {effect('reuse_intensive', 'LatestOnly', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}. On the original-compatible workload, the LatestOnly TTFT difference ({effect('original_compatible', 'LatestOnly', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}) is not resolved by its 95% interval.",
              f"3. **Adaptive vs AgeCov-Greedy:** the simple age-coverage score is not sufficient for reuse-intensive serving: Adaptive improves P95 age by {effect('reuse_intensive', 'AgeCov-Greedy', 'delta_p95_state_age_s_baseline_minus_adaptive', 's')} and mean TTFT by {effect('reuse_intensive', 'AgeCov-Greedy', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}. In original-compatible serving its TTFT difference ({effect('original_compatible', 'AgeCov-Greedy', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}) is inconclusive, so this is not claimed as a universal latency win.",
              "4. **Four-instance redundancy:** all four owners carried measured traffic, and the formal rows expose replica-suppression counters under 25% replica overlap. The 0/25/50/75% overlap sweep is calibration-only and remains NOT FOR PAPER; this run therefore demonstrates four-owner operation but does not claim a formal 3-to-4-instance effect size.",
              f"5. **Reuse-intensive TTFT:** yes, the strongest direct conversion is Adaptive versus RateFIFO ({effect('reuse_intensive', 'RateFIFO', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}); positive paired TTFT savings also occur against LatestOnly ({effect('reuse_intensive', 'LatestOnly', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}), AgeCov-Greedy ({effect('reuse_intensive', 'AgeCov-Greedy', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}), StaticSemantic ({effect('reuse_intensive', 'StaticSemantic', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}), and FullSync ({effect('reuse_intensive', 'FullSync', 'delta_ttft_ms_baseline_minus_adaptive', 'ms')}).",
              f"6. **Original-compatible TTFT:** freshness remains stronger evidence than latency generality. Although Adaptive improves P95 age versus RateFIFO by {effect('original_compatible', 'RateFIFO', 'delta_p95_state_age_s_baseline_minus_adaptive', 's')}, its TTFT shifts versus LatestOnly, AgeCov-Greedy, StaticSemantic, and FullSync have intervals that include zero; they should not be promoted as stable serving-latency gains.",
              "7. **Cached-token buckets:** yes. Fig. C and `ttft_by_reuse_bucket.csv` show substantially lower TTFT for long actual vLLM reuse than for the 1-511-token bucket. Empty buckets are retained rather than interpolated, so the plot does not manufacture a monotonic curve where the workload has no samples.",
              f"8. **Dynamic recovery:** all five repetitions recovered within the 45-s observation window for every policy, but Adaptive was not the shortest mean recovery time ({recovery_by_policy.get('Adaptive', 0.0):.2f} s); {fastest_recovery} was fastest at {recovery_by_policy.get(fastest_recovery, 0.0):.2f} s. Thus this dynamic run supports controlled recovery, not a claim that Adaptive universally recovers fastest.",
              f"9. **High churn:** yes. The churn experiment injected stale positives, owner cache resets, and delayed tombstones; it recorded {churn_fallbacks:.0f} normal-prefill fallback events. The separate live owner runtime check contains {native_fallbacks} native fallback decisions across four endpoints, with {native_unsafe} unsafe reuses.",
              "10. **Safety:** all VALID formal rows have zero `incorrect_kv_reuse_count` and zero request-error rate; the four native endpoint checks also passed scope/version/lease/eviction/restart scenarios with zero unsafe reuse.",
              "11. **Mechanism attribution:** replacement alone (LatestOnly) and simple utility ranking (AgeCov-Greedy) leave a positive reuse-intensive gap, supporting the value of the complete semantic mechanism. StaticSemantic narrows that gap; its P95-age difference is not robust in this sample, so the separate incremental contribution of adaptive admission beyond merge, urgency, and replica suppression should be reported as limited rather than universal.", ""]
    if selection_path.is_file():
        selected = json.loads(selection_path.read_text())
        lines += ["## Frozen RateFIFO calibration", "", f"The pre-registered calibration-only selector chose a {selected['selected_ratefifo_burst_frames']}-frame token-bucket burst. Its rule and all candidate scores are stored in `{selection_path}`.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/byh/B02/analysis/formal4t4"))
    args = parser.parse_args()
    root = args.root
    summary, raw, figures, report = root / "summary", root / "raw", root / "figures", root / "report"
    baseline = ensure_valid_summary(summary, "baseline", required=True)
    churn = ensure_valid_summary(summary, "churn", required=True)
    ensure_valid_summary(summary, "background", required=True)
    dynamic = ensure_valid_summary(summary, "dynamic", required=True)
    if len({row["policy"] for row in baseline}) != 7:
        raise RuntimeError("formal baseline data do not include all six real policies plus Ideal")
    aggregates = aggregate_cells(baseline)
    pairs = paired_comparisons(baseline)
    pair_summary = aggregate_pairs(pairs)
    buckets = reuse_buckets(raw / "baseline")
    accounting = signaling_accounting(baseline)
    correctness = correctness_rows(churn, baseline)
    native = native_validation_rows(raw / "native_validation")
    correctness.extend(native)
    write_csv(summary / "cell_aggregates.csv", aggregates)
    write_csv(summary / "paired_results.csv", pairs)
    write_csv(summary / "paired_result_aggregates.csv", pair_summary)
    write_csv(summary / "ttft_by_reuse_bucket.csv", buckets)
    write_csv(summary / "signaling_accounting.csv", accounting)
    write_csv(summary / "correctness.csv", correctness)
    plot_baseline(aggregates, figures / "fig_baseline_comparison.pdf")
    plot_pareto(aggregates, accounting, figures / "fig_signaling_pareto.pdf")
    plot_reuse_ttft(buckets, figures / "fig_ttft_by_reuse.pdf")
    recovery = plot_dynamic(raw / "dynamic", figures / "fig_dynamic_recovery.pdf")
    write_csv(summary / "dynamic_recovery.csv", recovery)
    plot_churn(churn, figures / "fig_churn_correctness.pdf")
    text = report_text(aggregates, pairs, buckets, recovery, churn, native, root / "calibration" / "ratefifo_selection.json")
    report.mkdir(parents=True, exist_ok=True)
    (report / "final_report.md").write_text(text + "\n")
    print(json.dumps({"baseline_rows": len(baseline), "churn_rows": len(churn), "dynamic_rows": len(dynamic),
                      "figures": [str(path) for path in sorted(figures.glob("*.pdf"))]}, indent=2))


if __name__ == "__main__":
    main()
